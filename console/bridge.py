"""Talk to the paired iPad through usbmuxd / lockdownd.

The iPad on this bench is a normal activated device. This module uses the
services Apple exposes to a trusted host: lockdown, the media filesystem (AFC),
app containers, installation, diagnostics, backup, profiles and an official
IPSW restore. It does not open the sealed system partition.
"""

from __future__ import annotations

import asyncio
import contextlib
import plistlib
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from ipsw_parser.ipsw import IPSW

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import ConnectionTerminatedError
from pymobiledevice3.irecv import IRecv, IRecvNoDeviceConnectedError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.restore.base_restore import Behavior
from pymobiledevice3.restore.device import Device
from pymobiledevice3.restore.restore import Restore
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.mobile_config import MobileConfigService
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
from pymobiledevice3.services.screenshot import ScreenshotService
from pymobiledevice3.services.springboard import SpringBoardServicesService

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BACKUP_DIR = DATA / "backups"
IPSW_DIR = DATA / "ipsw"
RELAY_DIR = DATA / "relay"
IPA_INBOX = DATA / "ipa"
LABEL = "ipad-console"

SCOPES = {"media", "crashes", "appdocs", "appbox", "root"}
RELAY_SOURCES = {
    "Baseband",
    "CrashReporter",
    "MobileAsset",
    "VARFS",
    "HFSMeta",
    "Lockdown",
    "MobileBackup",
    "MobileDelete",
    "MobileInstallation",
    "MobileNotes",
    "Network",
    "UserDatabases",
    "WiFi",
    "SystemConfiguration",
    "Ubiquity",
    "tmp",
}
APP_ATTRS = [
    "CFBundleIdentifier",
    "CFBundleDisplayName",
    "CFBundleName",
    "CFBundleShortVersionString",
    "CFBundleVersion",
    "ApplicationType",
    "UIFileSharingEnabled",
    "StaticDiskUsage",
    "DynamicDiskUsage",
    "MinimumOSVersion",
]
OVERVIEW_KEYS = [
    "DeviceName",
    "ProductType",
    "ProductVersion",
    "BuildVersion",
    "HardwareModel",
    "ModelNumber",
    "RegionInfo",
    "SerialNumber",
    "UniqueDeviceID",
    "UniqueChipID",
    "DeviceClass",
    "CPUArchitecture",
    "FirmwareVersion",
    "ActivationState",
    "PasswordProtected",
    "TimeZone",
    "BluetoothAddress",
    "WiFiAddress",
    "Language",
    "Locale",
    "ProductName",
    "DeviceColor",
    "BoardId",
    "ChipID",
]

_wall: dict[str, Any] = {"t": 0.0, "data": None}


def ensure_dirs() -> None:
    for path in (DATA, BACKUP_DIR, IPSW_DIR, RELAY_DIR, IPA_INBOX, IPA_INBOX / "done", IPA_INBOX / "failed"):
        path.mkdir(parents=True, exist_ok=True)


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        if len(value) > 64:
            return {"bytes": len(value)}
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def clean_path(path: str) -> str:
    raw = path or "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Путь не может подниматься выше корня")
        parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def safe_name(name: str) -> str:
    base = Path(name or "").name
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        raise ValueError("Некорректное имя файла")
    return base


def resolve_ipsw(token: str) -> str:
    token = (token or "").strip()
    if token.startswith("https://"):
        return token
    path = (IPSW_DIR / safe_name(token)).resolve()
    if path.parent != IPSW_DIR.resolve() or not path.is_file():
        raise ValueError("Файл прошивки не найден в data/ipsw")
    return str(path)


@contextlib.asynccontextmanager
async def lockdown():
    client = await create_using_usbmux(autopair=False, label=LABEL)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.close()


@contextlib.asynccontextmanager
async def afc_for(scope: str, bundle: Optional[str] = None):
    if scope not in SCOPES:
        raise ValueError("Неизвестная область файлов")
    if scope in {"appdocs", "appbox"} and not bundle:
        raise ValueError("Нужен идентификатор программы")
    async with lockdown() as client:
        afc = None
        try:
            if scope == "media":
                afc = AfcService(client)
                await afc.__aenter__()
            elif scope == "crashes":
                afc = AfcService(client, service_name="com.apple.crashreportcopymobile")
                await afc.__aenter__()
            elif scope == "root":
                afc = AfcService(client, service_name="com.apple.afc2")
                await afc.__aenter__()
            else:
                afc = await HouseArrestService.create(
                    client, bundle or "", documents_only=(scope == "appdocs")
                )
            yield afc
        finally:
            if afc is not None:
                with contextlib.suppress(Exception):
                    await afc.aclose()


def _child(parent: str, name: str) -> str:
    if parent == "/":
        return "/" + name
    return parent.rstrip("/") + "/" + name


async def list_dir(scope: str, path: str, bundle: Optional[str] = None) -> dict[str, Any]:
    path = clean_path(path)
    async with afc_for(scope, bundle) as afc:
        names = await afc.listdir(path)
        rows = []
        for name in names:
            if name in (".", ".."):
                continue
            full = _child(path, name)
            row: dict[str, Any] = {"name": name, "path": full, "dir": False, "size": None, "mtime": None}
            try:
                st = await afc.stat(full)
                row["dir"] = st.get("st_ifmt") == "S_IFDIR"
                row["link"] = st.get("st_ifmt") == "S_IFLNK"
                row["size"] = None if row["dir"] else st.get("st_size")
                mtime = st.get("st_mtime")
                row["mtime"] = mtime.isoformat(sep=" ", timespec="seconds") if mtime else None
            except Exception as exc:
                row["error"] = str(exc)
            rows.append(row)
    rows.sort(key=lambda item: (not item["dir"], item["name"].lower()))
    return {"path": path, "scope": scope, "bundle": bundle or "", "entries": rows}


async def stat_file(scope: str, path: str, bundle: Optional[str] = None) -> dict[str, Any]:
    path = clean_path(path)
    async with afc_for(scope, bundle) as afc:
        st = await afc.stat(path)
    return {
        "path": path,
        "dir": st.get("st_ifmt") == "S_IFDIR",
        "size": int(st.get("st_size") or 0),
    }


async def read_chunks(scope: str, path: str, bundle: Optional[str] = None) -> AsyncIterator[bytes]:
    path = clean_path(path)
    async with afc_for(scope, bundle) as afc:
        handle = await afc.fopen(path, "r")
        try:
            while True:
                chunk = await afc.fread(handle, 256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            with contextlib.suppress(Exception):
                await afc.fclose(handle)


async def write_upload(
    scope: str,
    directory: str,
    filename: str,
    chunks: AsyncIterator[bytes],
    bundle: Optional[str] = None,
) -> str:
    directory = clean_path(directory)
    dest = _child(directory, safe_name(filename))
    async with afc_for(scope, bundle) as afc:
        if directory != "/":
            await afc.makedirs(directory)
        handle = await afc.fopen(dest, "w")
        try:
            async for chunk in chunks:
                if chunk:
                    await afc.fwrite(handle, chunk)
        finally:
            await afc.fclose(handle)
    return dest


async def make_dir(scope: str, path: str, bundle: Optional[str] = None) -> str:
    path = clean_path(path)
    if path == "/":
        raise ValueError("Корень уже существует")
    async with afc_for(scope, bundle) as afc:
        await afc.makedirs(path)
    return path


async def remove_path(scope: str, path: str, bundle: Optional[str] = None) -> None:
    path = clean_path(path)
    if path == "/":
        raise ValueError("Корень удалять нельзя")
    async with afc_for(scope, bundle) as afc:
        failed = await afc.rm(path, force=True)
    if failed:
        raise RuntimeError("Не удалось удалить: " + ", ".join(failed[:8]))


async def move_path(scope: str, src: str, dst: str, bundle: Optional[str] = None) -> str:
    src = clean_path(src)
    dst = clean_path(dst)
    if src == "/" or dst == "/":
        raise ValueError("Корень перемещать нельзя")
    async with afc_for(scope, bundle) as afc:
        await afc.rename(src, dst)
    return dst


async def device_name() -> str:
    async with lockdown() as client:
        return str(await client.get_value(key="DeviceName") or "")


async def assert_confirm(confirm: str) -> str:
    name = await device_name()
    if (confirm or "").strip() != name:
        raise ValueError(f"Для подтверждения введите имя устройства: {name}")
    return name


async def overview() -> dict[str, Any]:
    async with lockdown() as client:
        values = await client.get_value() or {}
        battery = await client.get_value("com.apple.mobile.battery") or {}
        disk = await client.get_value("com.apple.disk_usage") or {}
        wifi_lockdown = None
        backup_encrypted = None
        with contextlib.suppress(Exception):
            wifi_lockdown = bool(await client.get_enable_wifi_connections())
        with contextlib.suppress(Exception):
            backup_encrypted = bool(await client.get_value("com.apple.mobile.backup", "WillEncrypt"))
        clock = None
        with contextlib.suppress(Exception):
            clock = (await client.get_date()).isoformat(sep=" ", timespec="seconds")
        health = None
        async with DiagnosticsService(client) as diag:
            with contextlib.suppress(Exception):
                health = await diag.get_battery()
    picked = {key: scrub(values.get(key)) for key in OVERVIEW_KEYS}
    temp = None
    cycles = None
    voltage = None
    design = None
    full = None
    if isinstance(health, dict):
        if health.get("Temperature") is not None:
            temp = round(health["Temperature"] / 100, 1)
        cycles = health.get("CycleCount")
        voltage = health.get("Voltage")
        design = health.get("DesignCapacity")
        full = health.get("AppleRawMaxCapacity") or health.get("NominalChargeCapacity")
    wear = None
    if design and full:
        wear = round(100 * float(full) / float(design))
    return {
        "identity": picked,
        "time": clock,
        "wifi_lockdown": wifi_lockdown,
        "backup_encrypted": backup_encrypted,
        "battery": scrub(battery),
        "disk": scrub(disk),
        "cell": {
            "temperature_c": temp,
            "cycles": cycles,
            "voltage_mv": voltage,
            "design_mah": design,
            "full_mah": full,
            "health_percent": wear,
        },
    }


async def set_name(name: str) -> None:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("Имя должно быть от 1 до 64 символов")
    async with lockdown() as client:
        await client.set_value(name, key="DeviceName")


async def set_locale(language: str, locale: str) -> None:
    async with lockdown() as client:
        if language.strip():
            await client.set_value(language.strip(), key="Language")
        if locale.strip():
            await client.set_value(locale.strip(), key="Locale")


async def set_wifi(enabled: bool) -> None:
    async with lockdown() as client:
        await client.set_enable_wifi_connections(enabled)


async def power(action: str) -> None:
    async with lockdown() as client:
        async with DiagnosticsService(client) as diag:
            try:
                if action == "restart":
                    await diag.restart()
                elif action == "shutdown":
                    await diag.shutdown()
                elif action == "sleep":
                    await diag.sleep()
                else:
                    raise ValueError("Неизвестная команда питания")
            except ConnectionTerminatedError:
                return


async def apps(kind: str = "User") -> list[dict[str, Any]]:
    if kind not in {"User", "System", "Any"}:
        raise ValueError("Тип программ: User, System или Any")
    async with lockdown() as client:
        async with InstallationProxyService(client) as proxy:
            raw = await proxy.lookup({"ApplicationType": kind, "ReturnAttributes": APP_ATTRS}) or {}
    rows = []
    for bundle, meta in raw.items():
        meta = meta or {}
        rows.append(
            {
                "id": bundle,
                "name": meta.get("CFBundleDisplayName") or meta.get("CFBundleName") or bundle,
                "version": meta.get("CFBundleShortVersionString") or "",
                "build": meta.get("CFBundleVersion") or "",
                "type": meta.get("ApplicationType") or kind,
                "sharing": bool(meta.get("UIFileSharingEnabled")),
                "min_os": meta.get("MinimumOSVersion") or "",
                "static_bytes": meta.get("StaticDiskUsage"),
                "dynamic_bytes": meta.get("DynamicDiskUsage"),
            }
        )
    rows.sort(key=lambda item: item["name"].lower())
    return rows


async def uninstall(bundle_id: str) -> None:
    bundle_id = (bundle_id or "").strip()
    if not bundle_id:
        raise ValueError("Нет идентификатора")
    async with lockdown() as client:
        async with InstallationProxyService(client) as proxy:
            found = await proxy.lookup(
                {
                    "BundleIDs": [bundle_id],
                    "ApplicationType": "Any",
                    "ReturnAttributes": ["ApplicationType", "CFBundleDisplayName"],
                }
            ) or {}
            meta = found.get(bundle_id) or {}
            if meta.get("ApplicationType") != "User":
                raise ValueError("Системную программу удалить нельзя")
            await proxy.uninstall(bundle_id)


def explain_install_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(mark in text for mark in ("ApplicationVerificationFailed", "0xE8008015", "0xE8008001", "BundleVerificationFailed")) or "signature" in lowered:
        return (
            "Айпад отклонил подпись этого IPA. Пройдёт пакет, подписанный для этого устройства. "
            "Разблокируйте экран. Если подписи нет, нужен джейлбрейк и AppSync — кнопка checkra1n на вкладке «Скорость»."
        )
    if "PasswordProtected" in text or "locked" in lowered:
        return "Разблокируйте айпад и не гасите экран, пока установка не закончится."
    if any(mark in text for mark in ("PackageExtractionFailed", "not a valid")) or "payload" in lowered or "zip" in lowered:
        return "Это не целый IPA. Файл должен быть zip-пакетом с папкой Payload."
    return text or exc.__class__.__name__


async def install_ipa(path: Path, on_progress) -> None:
    try:
        async with lockdown() as client:
            async with InstallationProxyService(client) as proxy:
                await proxy.install_from_local(path, handler=lambda pct, *_: on_progress(pct))
    except Exception as exc:
        raise RuntimeError(explain_install_error(exc)) from exc


async def icon_png(bundle_id: str) -> Optional[bytes]:
    async with lockdown() as client:
        async with SpringBoardServicesService(client) as spring:
            data = await spring.get_icon_pngdata(bundle_id)
    return data or None


async def wallpaper_png() -> Optional[bytes]:
    now = time.time()
    if _wall["data"] is not None and now - _wall["t"] < 60:
        return _wall["data"]
    async with lockdown() as client:
        async with SpringBoardServicesService(client) as spring:
            data = await spring.get_wallpaper_pngdata()
    _wall["data"] = data or None
    _wall["t"] = now
    return _wall["data"]


async def screenshot_png() -> bytes:
    async with lockdown() as client:
        async with ScreenshotService(client) as shot:
            data = await shot.take_screenshot()
    if not data:
        raise RuntimeError("Устройство не вернуло снимок")
    return data


async def profiles() -> dict[str, Any]:
    async with lockdown() as client:
        async with MobileConfigService(client) as config:
            payload = await config.get_profile_list()
    return scrub(payload)


async def install_profile(payload: bytes) -> None:
    if not payload:
        raise ValueError("Пустой профиль")
    async with lockdown() as client:
        async with MobileConfigService(client) as config:
            await config.install_profile(payload)


async def remove_profile(identifier: str) -> None:
    identifier = (identifier or "").strip()
    if not identifier:
        raise ValueError("Нет идентификатора профиля")
    async with lockdown() as client:
        async with MobileConfigService(client) as config:
            await config.remove_profile(identifier)


async def erase_device() -> None:
    async with lockdown() as client:
        async with MobileConfigService(client) as config:
            try:
                await config.erase_device(preserve_data_plan=False, disallow_proximity_setup=False)
            except ConnectionTerminatedError:
                return


async def enter_recovery() -> None:
    async with lockdown() as client:
        try:
            await client.enter_recovery()
        except ConnectionTerminatedError:
            return


def leave_recovery() -> None:
    try:
        irecv = IRecv(timeout=2)
    except IRecvNoDeviceConnectedError as exc:
        raise RuntimeError("В recovery/DFU сейчас никто не виден") from exc
    irecv.set_autoboot(True)
    irecv.reboot()


async def connection() -> dict[str, Any]:
    devices = [dev for dev in await usbmux.list_devices() if dev.connection_type == "USB"]
    if devices:
        try:
            async with lockdown() as client:
                name = await client.get_value(key="DeviceName")
                product = await client.get_value(key="ProductType")
                version = await client.get_value(key="ProductVersion")
                udid = client.udid
            return {
                "connected": True,
                "mode": "normal",
                "name": name,
                "product": product,
                "version": version,
                "udid": udid,
            }
        except Exception as exc:
            return {"connected": True, "mode": "usb", "error": str(exc), "udid": devices[0].serial}
    try:
        irecv = await asyncio.to_thread(IRecv, timeout=1)
    except Exception:
        return {"connected": False, "mode": "absent"}
    return {
        "connected": True,
        "mode": irecv.mode.name.lower(),
        "product": irecv.product_type,
        "name": irecv.display_name,
        "ecid": irecv.ecid,
    }


def signed_firmwares(product: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"https://api.ipsw.me/v4/device/{product}",
        params={"type": "ipsw"},
        headers={"Accept": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    body = response.json()
    rows = []
    for item in body.get("firmwares") or []:
        rows.append(
            {
                "version": item.get("version"),
                "build": item.get("buildid"),
                "signed": bool(item.get("signed")),
                "url": item.get("url"),
                "size": item.get("filesize"),
                "released": item.get("releasedate"),
                "sha1": item.get("sha1"),
            }
        )
    rows.sort(key=lambda item: item.get("released") or "", reverse=True)
    rows.sort(key=lambda item: not item["signed"])
    return rows


def local_ipsws() -> list[dict[str, Any]]:
    ensure_dirs()
    rows = []
    for path in sorted(IPSW_DIR.glob("*.ipsw")):
        stat = path.stat()
        rows.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
            }
        )
    return rows


def download_ipsw(url: str, dest: Path, progress) -> None:
    if not url.startswith("https://"):
        raise ValueError("Нужна ссылка https")
    ensure_dirs()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        got = 0
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                got += len(chunk)
                progress(got, total)
    tmp.replace(dest)


async def backup(password: str, progress) -> None:
    ensure_dirs()
    async with lockdown() as client:
        async with Mobilebackup2Service(client) as service:
            await service.backup(
                full=True,
                backup_directory=BACKUP_DIR,
                password=password or "",
                progress_callback=progress,
            )


async def restore_backup(password: str, source: str, progress) -> None:
    ensure_dirs()
    async with lockdown() as client:
        udid = source or client.udid
        folder = BACKUP_DIR / udid
        if not (folder / "Manifest.plist").is_file():
            raise ValueError("Копия для этого устройства не найдена")
        async with Mobilebackup2Service(client) as service:
            await service.restore(
                backup_directory=BACKUP_DIR,
                source=udid,
                password=password or "",
                progress_callback=progress,
                reboot=True,
                copy=False,
                settings=True,
                remove=False,
                system=False,
                skip_apps=False,
            )


def list_backups() -> list[dict[str, Any]]:
    ensure_dirs()
    rows = []
    for child in sorted(BACKUP_DIR.iterdir() if BACKUP_DIR.exists() else []):
        manifest_path = child / "Manifest.plist"
        if not manifest_path.is_file():
            continue
        with manifest_path.open("rb") as handle:
            manifest = plistlib.load(handle)
        info: dict[str, Any] = {}
        info_path = child / "Info.plist"
        if info_path.is_file():
            with info_path.open("rb") as handle:
                info = plistlib.load(handle)
        rows.append(
            {
                "udid": child.name,
                "device": info.get("Device Name") or info.get("Display Name"),
                "version": info.get("Product Version"),
                "encrypted": bool(manifest.get("IsEncrypted")),
                "when": scrub(info.get("Last Backup Date")),
            }
        )
    return rows


async def relay(sources: list[str], dest: Path) -> None:
    unknown = [item for item in sources if item not in RELAY_SOURCES]
    if unknown:
        raise ValueError("Неизвестный источник: " + ", ".join(unknown))
    if not sources:
        raise ValueError("Выберите хотя бы один источник")
    from pymobiledevice3.services.file_relay import FileRelayService

    async with lockdown() as client:
        async with FileRelayService(client) as relay_service:
            blob = await relay_service.request_sources(sources)
    if not blob:
        raise RuntimeError("Устройство не отдало архив")
    ensure_dirs()
    dest.write_bytes(blob)


async def flash(token: str, erase: bool, on_log) -> None:
    """Install a signed Apple IPSW. Erase wipes the device; update keeps data."""
    import logging

    class _Tap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            on_log(record.getMessage())

    tap = _Tap()
    tap.setLevel(logging.INFO)
    loggers = [logging.getLogger(name) for name in ("pymobiledevice3", "ipsw_parser")]
    for logger in loggers:
        logger.addHandler(tap)
        logger.setLevel(logging.INFO)
    try:
        ipsw = IPSW.create_from_path(resolve_ipsw(token))
        async with lockdown() as client:
            device = Device(lockdown=client)
            behavior = Behavior.Erase if erase else Behavior.Update
            await Restore(ipsw, device, behavior=behavior).update()
    finally:
        for logger in loggers:
            logger.removeHandler(tap)


def relay_files() -> list[dict[str, Any]]:
    ensure_dirs()
    rows = []
    for path in sorted(RELAY_DIR.glob("*.cpio.gz"), reverse=True):
        stat = path.stat()
        rows.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
            }
        )
    return rows


TOOLS = DATA / "tools"
CHECKRA1N_URL = (
    "https://assets.checkra.in/downloads/linux/cli/x86_64/"
    "b0edbb87a5e084caf35795dcb3b088146ad5457235940f83e007f59ca57b319c/checkra1n-x86_64"
)
CHECKRA1N_SHA = "b0edbb87a5e084caf35795dcb3b088146ad5457235940f83e007f59ca57b319c"
CHECKRA1N_BIN = TOOLS / "checkra1n"
CACHE_DIRS = ("/tmp", "/Library/Caches", "/Library/SplashBoard")

# iPad Air is A7. On Linux, checkra1n 0.12.x hangs in the checkm8 setup.
# The iOS CFW guide points A7 hosts at official 0.10.1, then a quick replug
# when the log says "Right before trigger".
A7_PRODUCTS = {"iPad4,1", "iPad4,2", "iPad4,3", "iPad4,4", "iPad4,5", "iPad4,6", "iPhone6,1", "iPhone6,2"}


def lighten_profile() -> bytes:
    """A normal configuration profile: no Siri, no Spotlight web, no diagnostics."""
    import uuid

    content = {
        "PayloadDisplayName": "iZOOM легче",
        "PayloadIdentifier": "local.izoom.lighten",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": str(uuid.uuid4()),
        "PayloadVersion": 1,
        "PayloadContent": [
            {
                "PayloadDisplayName": "Ограничения",
                "PayloadIdentifier": "local.izoom.lighten.access",
                "PayloadType": "com.apple.applicationaccess",
                "PayloadUUID": str(uuid.uuid4()),
                "PayloadVersion": 1,
                "allowAssistant": False,
                "allowSpotlightInternetResults": False,
                "allowDiagnosticSubmission": False,
            }
        ],
    }
    return plistlib.dumps(content)


async def purge_caches(progress) -> dict[str, Any]:
    """Drop regenerable caches inside each user app. Documents stay."""
    listed = await apps("User")
    cleared = 0
    skipped: list[str] = []
    total = max(1, len(listed))
    for index, app in enumerate(listed):
        progress(int(index * 100 / total), app["name"])
        try:
            async with afc_for("appbox", app["id"]) as afc:
                for path in CACHE_DIRS:
                    with contextlib.suppress(Exception):
                        await afc.rm(path, force=True)
                    with contextlib.suppress(Exception):
                        await afc.makedirs(path)
            cleared += 1
        except Exception as exc:
            skipped.append(f"{app['name']}: {exc}")
    progress(100, "готово")
    return {"cleared": cleared, "total": len(listed), "skipped": skipped[:15]}


async def heavy_apps() -> list[dict[str, Any]]:
    async with lockdown() as client:
        async with InstallationProxyService(client) as proxy:
            raw = await proxy.get_apps(application_type="User", calculate_sizes=True)
    rows = []
    for bundle, meta in (raw or {}).items():
        meta = meta or {}
        static = int(meta.get("StaticDiskUsage") or 0)
        dynamic = int(meta.get("DynamicDiskUsage") or 0)
        rows.append(
            {
                "id": bundle,
                "name": meta.get("CFBundleDisplayName") or meta.get("CFBundleName") or bundle,
                "version": meta.get("CFBundleShortVersionString") or "",
                "static_bytes": static,
                "dynamic_bytes": dynamic,
                "total_bytes": static + dynamic,
            }
        )
    rows.sort(key=lambda item: item["total_bytes"], reverse=True)
    return rows


async def _port_open(port: int) -> bool:
    from pymobiledevice3.service_connection import ServiceConnection

    try:
        conn = await asyncio.wait_for(ServiceConnection.create_using_usbmux(None, port), timeout=4)
    except Exception:
        return False
    with contextlib.suppress(Exception):
        await conn.aclose()
    return True


async def workshop() -> dict[str, Any]:
    product = None
    version = None
    afc2 = False
    cydia = False
    try:
        async with lockdown() as client:
            product = await client.get_value(key="ProductType")
            version = await client.get_value(key="ProductVersion")
            try:
                async with AfcService(client, service_name="com.apple.afc2") as afc:
                    await afc.listdir("/")
                    afc2 = True
            except Exception:
                afc2 = False
            async with InstallationProxyService(client) as proxy:
                found = await proxy.lookup(
                    {
                        "BundleIDs": ["com.saurik.Cydia", "org.coolstar.cydia", "xyz.willy.Zebra"],
                        "ApplicationType": "Any",
                        "ReturnAttributes": ["CFBundleIdentifier"],
                    }
                )
                cydia = bool(found)
    except Exception as exc:
        return {"connected": False, "error": str(exc), "checkra1n": CHECKRA1N_BIN.is_file()}
    ssh = await _port_open(44)
    a7 = product in A7_PRODUCTS
    return {
        "connected": True,
        "product": product,
        "version": version,
        "jailbroken": bool(afc2 or cydia or ssh),
        "afc2": afc2,
        "cydia": cydia,
        "ssh": ssh,
        "a7": a7,
        "checkra1n": CHECKRA1N_BIN.is_file(),
        "ram": "1 ГБ" if a7 else None,
        "chip": "A7" if a7 else None,
    }


def ensure_checkra1n() -> Path:
    import hashlib

    ensure_dirs()
    TOOLS.mkdir(parents=True, exist_ok=True)
    if CHECKRA1N_BIN.is_file():
        digest = hashlib.sha256(CHECKRA1N_BIN.read_bytes()).hexdigest()
        if digest == CHECKRA1N_SHA:
            CHECKRA1N_BIN.chmod(0o755)
            return CHECKRA1N_BIN
    tmp = CHECKRA1N_BIN.with_suffix(".part")
    with requests.get(CHECKRA1N_URL, timeout=60, stream=True) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(1024 * 256):
                if chunk:
                    handle.write(chunk)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != CHECKRA1N_SHA:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Контрольная сумма checkra1n не сошлась, файл удалён")
    tmp.replace(CHECKRA1N_BIN)
    CHECKRA1N_BIN.chmod(0o755)
    return CHECKRA1N_BIN


def run_checkra1n(on_line) -> int:
    """Stream official checkra1n 0.10.1. The iPad must be put in DFU by hand."""
    import os
    import pty
    import select
    import subprocess

    binary = ensure_checkra1n()
    on_line("checkra1n 0.10.1. Зажмите питание и Home на 10 секунд,")
    on_line("отпустите питание и держите Home, пока экран не станет чёрным.")
    on_line("Когда в логе будет Right before trigger — выдерните кабель на секунду и сразу воткните.")
    master, slave = pty.openpty()
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        [str(binary), "-c", "-v", "-n"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=env,
    )
    os.close(slave)
    deadline = time.time() + 15 * 60
    buf = b""
    try:
        while time.time() < deadline:
            if proc.poll() is not None and not select.select([master], [], [], 0)[0]:
                break
            ready, _, _ = select.select([master], [], [], 0.4)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf or b"\r" in buf:
                cut = min([i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0])
                line = buf[:cut].decode("utf-8", "replace")
                buf = buf[cut + 1 :]
                clean = "".join(ch for ch in line if ch.isprintable() or ch == " ").strip()
                if clean:
                    on_line(clean[-240:])
        if proc.poll() is None:
            proc.terminate()
            on_line("checkra1n остановлен по таймеру 15 минут")
        return proc.wait(timeout=5)
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()

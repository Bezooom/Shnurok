"""Web console for the iPad on this USB port. Binds to localhost only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from console import bridge
from pymobiledevice3.services.syslog import SyslogService

log = logging.getLogger("ipad-console")
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="iZOOM", docs_url=None, redoc_url=None)
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = asyncio.Lock()
_tasks: set[asyncio.Task[Any]] = set()


class PowerBody(BaseModel):
    action: str


class NameBody(BaseModel):
    name: str


class LocaleBody(BaseModel):
    language: str = ""
    locale: str = ""


class WifiBody(BaseModel):
    enabled: bool


class PathBody(BaseModel):
    scope: str = "media"
    path: str = "/"
    bundle: str = ""


class RenameBody(PathBody):
    to: str


class UninstallBody(BaseModel):
    bundle_id: str


class ConfirmBody(BaseModel):
    confirm: str = ""


class RestoreBody(BaseModel):
    ipsw: str
    erase: bool = False
    confirm: str


class DownloadBody(BaseModel):
    url: str
    name: str = ""


class BackupBody(BaseModel):
    password: str = ""


class BackupRestoreBody(BaseModel):
    confirm: str
    password: str = ""
    udid: str = ""


class RelayBody(BaseModel):
    sources: list[str] = Field(default_factory=list)


class ProfileBody(BaseModel):
    identifier: str


class RecoveryBody(BaseModel):
    action: str


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse({"error": detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def boom(_request, exc: Exception):
    log.exception("request failed")
    return JSONResponse({"error": str(exc) or exc.__class__.__name__}, status_code=500)


def _exclusive_running() -> bool:
    return any(job.get("status") == "running" and job.get("exclusive") for job in jobs.values())


async def start_job(name: str, worker, exclusive: bool = False) -> dict[str, Any]:
    async with jobs_lock:
        if exclusive and _exclusive_running():
            raise HTTPException(409, "Уже идёт длинная операция. Дождитесь её конца.")
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "name": name,
            "status": "running",
            "progress": 0,
            "message": "Запущено",
            "lines": [],
            "error": None,
            "exclusive": exclusive,
            "result": None,
            "started": time.time(),
        }
        jobs[job_id] = job

    async def runner():
        try:
            await worker(job)
            if job["status"] == "running":
                job["status"] = "done"
                job["progress"] = 100
                if job["message"] == "Запущено":
                    job["message"] = "Готово"
        except Exception as exc:
            log.exception("job %s failed", name)
            job["status"] = "error"
            job["error"] = str(exc) or exc.__class__.__name__
            job["message"] = job["error"]

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"id": job_id, "name": name}


def note(job: dict[str, Any], text: str) -> None:
    job["message"] = text
    job["lines"].append(text)
    del job["lines"][:-80]


@app.on_event("startup")
async def _startup() -> None:
    bridge.ensure_dirs()
    logging.basicConfig(level=logging.INFO)
    task = asyncio.create_task(watch_ipa_inbox())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_ipa_inbox() -> None:
    """Install any .ipa dropped into data/ipa once the copy has finished."""
    seen: set[tuple[str, int]] = set()
    while True:
        await asyncio.sleep(2)
        if _exclusive_running():
            continue
        for path in sorted(bridge.IPA_INBOX.glob("*.ipa")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (path.name, stat.st_size)
            if key not in seen:
                seen.add(key)
                continue
            seen.discard(key)
            if stat.st_size <= 0 or time.time() - stat.st_mtime < 1:
                seen.add(key)
                continue
            await enqueue_ipa(path)
            break


async def enqueue_ipa(path: Path) -> dict[str, Any]:
    name = path.name

    async def worker(job: dict[str, Any]):
        try:
            note(job, f"Ставлю {name}. Разблокируйте айпад и не гасите экран.")

            def on_progress(pct: Any):
                try:
                    job["progress"] = int(pct or 0)
                except (TypeError, ValueError):
                    pass

            await bridge.install_ipa(path, on_progress)
            dest = bridge.IPA_INBOX / "done" / name
            if dest.exists():
                dest = bridge.IPA_INBOX / "done" / f"{path.stem}-{job['id']}.ipa"
            path.replace(dest)
            note(job, "Программа установлена")
        except Exception as exc:
            message = bridge.explain_install_error(exc)
            failed = bridge.IPA_INBOX / "failed"
            failed.mkdir(parents=True, exist_ok=True)
            if path.exists():
                target = failed / name
                if target.exists():
                    target = failed / f"{path.stem}-{job['id']}.ipa"
                path.replace(target)
            (failed / f"{Path(name).stem}.txt").write_text(message, encoding="utf-8")
            raise RuntimeError(message) from exc

    return await start_job(f"IPA {name}", worker, exclusive=True)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    return await bridge.connection()


@app.get("/api/overview")
async def overview():
    return await bridge.overview()


@app.post("/api/power")
async def power(body: PowerBody):
    await bridge.power(body.action)
    return {"ok": True}


@app.post("/api/name")
async def rename(body: NameBody):
    await bridge.set_name(body.name)
    return {"ok": True}


@app.post("/api/locale")
async def locale(body: LocaleBody):
    await bridge.set_locale(body.language, body.locale)
    return {"ok": True, "note": "Язык применится после перезагрузки"}


@app.post("/api/wifi")
async def wifi(body: WifiBody):
    await bridge.set_wifi(body.enabled)
    return {"ok": True}


@app.get("/api/fs/list")
async def fs_list(scope: str = "media", path: str = "/", bundle: str = ""):
    return await bridge.list_dir(scope, path, bundle or None)


@app.get("/api/fs/download")
async def fs_download(scope: str = "media", path: str = "/", bundle: str = "", attachment: int = 0):
    info = await bridge.stat_file(scope, path, bundle or None)
    if info["dir"]:
        raise HTTPException(400, "Это папка")
    media = mimetypes.guess_type(info["path"])[0] or "application/octet-stream"
    filename = Path(info["path"]).name
    disposition = "attachment" if attachment else "inline"
    headers = {
        "Content-Length": str(info["size"]),
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
    }
    return StreamingResponse(
        bridge.read_chunks(scope, info["path"], bundle or None),
        media_type=media,
        headers=headers,
    )


@app.post("/api/fs/upload")
async def fs_upload(
    file: UploadFile = File(...),
    scope: str = Form("media"),
    path: str = Form("/"),
    bundle: str = Form(""),
):
    async def chunks():
        while True:
            blob = await file.read(256 * 1024)
            if not blob:
                break
            yield blob

    dest = await bridge.write_upload(scope, path, file.filename or "upload.bin", chunks(), bundle or None)
    return {"path": dest}


@app.post("/api/fs/mkdir")
async def fs_mkdir(body: PathBody):
    dest = await bridge.make_dir(body.scope, body.path, body.bundle or None)
    return {"path": dest}


@app.post("/api/fs/delete")
async def fs_delete(body: PathBody):
    await bridge.remove_path(body.scope, body.path, body.bundle or None)
    return {"ok": True}


@app.post("/api/fs/rename")
async def fs_rename(body: RenameBody):
    dest = await bridge.move_path(body.scope, body.path, body.to, body.bundle or None)
    return {"path": dest}


@app.get("/api/apps")
async def apps(kind: str = "User"):
    return {"apps": await bridge.apps(kind)}


@app.post("/api/apps/uninstall")
async def uninstall(body: UninstallBody):
    await bridge.uninstall(body.bundle_id)
    return {"ok": True}


@app.get("/api/ipa")
async def ipa_inbox():
    bridge.ensure_dirs()
    waiting = [path.name for path in sorted(bridge.IPA_INBOX.glob("*.ipa"))]
    return {"folder": str(bridge.IPA_INBOX), "waiting": waiting}


@app.post("/api/apps/install")
async def install(file: UploadFile = File(...)):
    bridge.ensure_dirs()
    filename = bridge.safe_name(file.filename or "app.ipa")
    if not filename.lower().endswith(".ipa"):
        filename += ".ipa"
    dest = bridge.IPA_INBOX / filename
    if dest.exists():
        dest = bridge.IPA_INBOX / f"{dest.stem}-{uuid.uuid4().hex[:6]}.ipa"
    with dest.open("wb") as handle:
        while True:
            blob = await file.read(1024 * 1024)
            if not blob:
                break
            handle.write(blob)
    # The inbox watcher installs the file. Touch mtime so the size looks settled.
    os_utime = dest.stat()
    if os_utime.st_size <= 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "Пустой файл")
    return {"ok": True, "file": dest.name, "folder": str(bridge.IPA_INBOX)}


@app.get("/api/apps/icon")
async def app_icon(bundle: str):
    data = await bridge.icon_png(bundle)
    if not data:
        raise HTTPException(404, "Нет значка")
    return Response(content=data, media_type="image/png")


@app.get("/api/wallpaper")
async def wallpaper():
    data = await bridge.wallpaper_png()
    if not data:
        raise HTTPException(404, "Обои недоступны")
    return Response(content=data, media_type="image/png")


@app.get("/api/screenshot")
async def screenshot():
    try:
        data = await bridge.screenshot_png()
    except Exception as exc:
        raise HTTPException(
            409,
            "Снимок экрана на iOS 12 просит образ разработчика, Apple его для этой системы уже не раздаёт. "
            f"({exc})",
        ) from exc
    return Response(content=data, media_type="image/png")


@app.websocket("/api/syslog")
async def syslog(socket: WebSocket):
    await socket.accept()
    try:
        async with bridge.lockdown() as client:
            async with SyslogService(client) as service:
                async for line in service.watch():
                    text = line if isinstance(line, str) else bytes(line).decode("utf-8", "replace")
                    await socket.send_text(text.rstrip("\n"))
    except WebSocketDisconnect:
        return
    except Exception as exc:
        with contextlib.suppress(Exception):
            await socket.send_text(f"[остановлено] {exc}")
            await socket.close()


@app.get("/api/profiles")
async def profile_list():
    return await bridge.profiles()


@app.post("/api/profiles/install")
async def profile_install(file: UploadFile = File(...)):
    await bridge.install_profile(await file.read())
    return {"ok": True, "note": "Подтвердите установку на экране айпада"}


@app.post("/api/profiles/remove")
async def profile_remove(body: ProfileBody):
    await bridge.remove_profile(body.identifier)
    return {"ok": True}


@app.post("/api/erase")
async def erase(body: ConfirmBody):
    await bridge.assert_confirm(body.confirm)

    async def worker(job: dict[str, Any]):
        note(job, "Стираю данные. Айпад перезагрузится.")
        await bridge.erase_device()
        note(job, "Команда принята")

    return await start_job("Стирание", worker, exclusive=True)


@app.post("/api/recovery")
async def recovery(body: RecoveryBody):
    if body.action == "enter":
        await bridge.enter_recovery()
        return {"ok": True}
    if body.action == "exit":
        await asyncio.to_thread(bridge.leave_recovery)
        return {"ok": True}
    raise HTTPException(400, "action: enter или exit")


@app.get("/api/os/firmwares")
async def firmwares():
    state = await bridge.connection()
    product = state.get("product") or "iPad4,1"
    if not str(product).startswith("iPad") and not str(product).startswith("iPhone"):
        product = "iPad4,1"
    rows = await asyncio.to_thread(bridge.signed_firmwares, product)
    return {"product": product, "firmwares": rows}


@app.get("/api/os/local")
async def local_firmware():
    return {"files": bridge.local_ipsws()}


@app.post("/api/os/download")
async def download_firmware(body: DownloadBody):
    url = body.url.strip()
    if not url.startswith("https://"):
        raise HTTPException(400, "Нужна ссылка https")
    name = bridge.safe_name(body.name or url.rsplit("/", 1)[-1] or "firmware.ipsw")
    if not name.endswith(".ipsw"):
        name += ".ipsw"
    dest = bridge.IPSW_DIR / name

    async def worker(job: dict[str, Any]):
        note(job, f"Качаю {name}")

        def progress(got: int, total: int) -> None:
            job["progress"] = int(got * 100 / total) if total else 0
            job["message"] = f"{got // (1024 * 1024)} МБ" + (f" из {total // (1024 * 1024)}" if total else "")

        await asyncio.to_thread(bridge.download_ipsw, url, dest, progress)
        job["result"] = {"name": name, "size": dest.stat().st_size}
        note(job, "Файл прошивки на диске")

    return await start_job(f"Загрузка {name}", worker, exclusive=True)


@app.post("/api/os/restore")
async def restore_os(body: RestoreBody):
    await bridge.assert_confirm(body.confirm)
    token = body.ipsw.strip()
    bridge.resolve_ipsw(token)
    kind = "Стирающая прошивка" if body.erase else "Обновление прошивки"

    async def worker(job: dict[str, Any]):
        note(job, "Не отключайте кабель. Айпад уйдёт в recovery сам.")
        await bridge.flash(token, body.erase, lambda line: note(job, line))
        note(job, "Прошивка завершена")

    return await start_job(kind, worker, exclusive=True)


@app.get("/api/backups")
async def backups():
    return {"backups": bridge.list_backups()}


@app.post("/api/backup")
async def backup(body: BackupBody):
    async def worker(job: dict[str, Any]):
        note(job, "Снимаю копию. Разблокируйте айпад, если он попросит.")

        def progress(pct: Any) -> None:
            try:
                job["progress"] = int(pct or 0)
            except (TypeError, ValueError):
                pass

        await bridge.backup(body.password, progress)
        note(job, "Копия готова")

    return await start_job("Резервная копия", worker, exclusive=True)


@app.post("/api/backup/restore")
async def backup_restore(body: BackupRestoreBody):
    await bridge.assert_confirm(body.confirm)

    async def worker(job: dict[str, Any]):
        note(job, "Возвращаю копию на айпад")

        def progress(pct: Any) -> None:
            try:
                job["progress"] = int(pct or 0)
            except (TypeError, ValueError):
                pass

        await bridge.restore_backup(body.password, body.udid, progress)
        note(job, "Копия развёрнута")

    return await start_job("Восстановление копии", worker, exclusive=True)


@app.get("/api/relay")
async def relay_list():
    return {"sources": sorted(bridge.RELAY_SOURCES), "files": bridge.relay_files()}


@app.post("/api/relay")
async def relay_pull(body: RelayBody):
    bridge.ensure_dirs()
    name = time.strftime("%Y%m%d-%H%M%S") + ".cpio.gz"
    dest = bridge.RELAY_DIR / name

    async def worker(job: dict[str, Any]):
        note(job, "Собираю архив на айпаде")
        await bridge.relay(body.sources, dest)
        job["result"] = {"name": name, "size": dest.stat().st_size}
        note(job, f"Архив {name}")

    return await start_job("Архив данных", worker, exclusive=True)


@app.get("/api/relay/download")
async def relay_download(name: str):
    path = (bridge.RELAY_DIR / bridge.safe_name(name)).resolve()
    if path.parent != bridge.RELAY_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Архив не найден")
    return FileResponse(path, filename=path.name, media_type="application/gzip")


@app.get("/api/workshop")
async def workshop():
    return await bridge.workshop()


@app.post("/api/speed/purge")
async def purge():
    async def worker(job: dict[str, Any]):
        def progress(pct: int, name: str) -> None:
            job["progress"] = pct
            note(job, name)

        result = await bridge.purge_caches(progress)
        job["result"] = result
        note(job, f"Почищено программ: {result['cleared']} из {result['total']}")

    return await start_job("Чистка кэшей", worker, exclusive=True)


@app.get("/api/speed/heavy")
async def heavy():
    return {"apps": await bridge.heavy_apps()}


@app.post("/api/speed/profile")
async def light_profile():
    await bridge.install_profile(bridge.lighten_profile())
    return {"ok": True, "note": "На айпаде нужно подтвердить профиль «iZOOM легче»"}


@app.post("/api/jailbreak")
async def jailbreak():
    async def worker(job: dict[str, Any]):
        note(job, "Беру официальный checkra1n 0.10.1")
        code = await asyncio.to_thread(bridge.run_checkra1n, lambda line: note(job, line))
        if code not in (0, None):
            raise RuntimeError(f"checkra1n завершился с кодом {code}")
        note(job, "checkra1n закончил. Если на айпаде появилось приложение loader, откройте его и поставьте Cydia.")

    return await start_job("checkra1n", worker, exclusive=True)


@app.get("/api/jobs")
async def job_list():
    rows = sorted(jobs.values(), key=lambda item: item["started"], reverse=True)
    return {"jobs": [{key: job[key] for key in ("id", "name", "status", "progress", "message", "error", "started", "result")} | {"lines": job["lines"][-12:]} for job in rows[:20]]}


@app.get("/api/jobs/{job_id}")
async def job_one(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job


app.mount("/static", StaticFiles(directory=STATIC), name="static")

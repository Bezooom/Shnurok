"""Linux USB quirks helper for checkm8 and pongoOS workflows.

Solves three common Linux host-controller failures:
1. Kernel driver `apple-mfi-fastcharge` greedily claiming interface 0.
2. ASM1142 / USB-C host controllers crashing A7 SecureROM on physical bus reset.
3. Post-exploit stalled status stage on control transfers.
"""

from __future__ import annotations

import glob
import os
import sys
import time

try:
    import usb.backend.libusb1 as l1
    import usb.core
    import usb.util
except ImportError:
    l1 = None
    usb = None


def unbind_fastcharge() -> list[str]:
    """Unbind apple-mfi-fastcharge from any connected Apple device."""
    unbound: list[str] = []
    driver_path = "/sys/bus/usb/drivers/apple-mfi-fastcharge"
    if not os.path.isdir(driver_path):
        return unbound

    for path in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(path, "idVendor"), encoding="utf-8") as f:
                if f.read().strip() != "05ac":
                    continue
        except OSError:
            continue

        name = os.path.basename(path)
        unbind_file = os.path.join(driver_path, "unbind")
        try:
            with open(unbind_file, "w", encoding="utf-8") as f:
                f.write(name)
            unbound.append(name)
        except OSError:
            pass

    return unbound


def dfu_abort(dev) -> bool:
    """Issue a DFU ABORT request (bRequest 6).

    On host controllers that cannot emit a clean 1-ms bus reset (e.g. ASMedia ASM1142),
    SecureROM treats DFU ABORT as a soft reset event, enabling re-enumeration
    without rebooting the hardware.
    """
    try:
        dev.ctrl_transfer(0x21, 6, 0, 0, None, timeout=2000)
        return True
    except Exception:
        return False


def boot_trigger(dev, force_abort: bool = True) -> None:
    """Trigger execution after image upload.

    Uses DFU ABORT when force_abort=True or when RESET_MODE=abort, avoiding
    libusb_reset_device() crashes on sensitive Apple Silicon / A7 setups.
    """
    mode = os.environ.get("RESET_MODE", "abort" if force_abort else "bus")
    if mode == "abort":
        dfu_abort(dev)
    else:
        try:
            dev.reset()
        except Exception:
            pass


def wait_for_device(id_vendor: int, id_product: int, timeout: float = 30.0):
    """Wait for a device to enumerate on the USB bus."""
    if usb is None:
        raise RuntimeError("pyusb is required for wait_for_device()")

    start = time.time()
    while time.time() - start < timeout:
        dev = usb.core.find(idVendor=id_vendor, idProduct=id_product)
        if dev is not None:
            return dev
        time.sleep(0.2)
    return None

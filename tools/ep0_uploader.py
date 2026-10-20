"""Host-side ramdisk uploader for the forked pongoOS control-transfer path.

Protocol (see fork/pongoos-usbloader-ep0.patch):

  OUT 0x21/1 wLength=4            resize upload buffer (stock pongoOS handler)
  OUT 0x21/1 wValue=idx wIndex=0  one data chunk, wLength <= 0x1000 (forked handler)
  IN  0xA1/3 len=6                DFU GETSTATUS (forked handler)
  OUT 0x21/3                      shell command on stdin (stock handler)

No CRC trailer is required: the forked handler appends bytes directly into
loader_xfer_recv_data, so `ramdisk` sees exactly the bytes we sent.

Run as root against a device in download mode (pongoOS enumerates as 05ac:4141).
"""

from __future__ import annotations

import struct
import sys
from typing import Protocol

EP0_DATA_OUT_MAX = 0x1000  # ep0_out.default_xfer_dma_size == DMA_BUFFER_SIZE
PONGO_VID, PONGO_PID = 0x5AC, 0x4141

STATUS_OK = 0
STATUS_ERR_MANIFEST = 8


class Transport(Protocol):
    """One USB control transfer. Returns the DATA IN stage bytes, if any."""

    def control(
        self,
        bmRequestType: int,
        bRequest: int,
        wValue: int = 0,
        wIndex: int = 0,
        data_or_wLength: bytes | int | None = None,
    ) -> bytes: ...


class UsbTransport:
    def __init__(self, device) -> None:
        self.device = device

    def control(self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None):
        return bytes(
            self.device.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex,
                                     data_or_wLength, timeout=10000)
        )


def resize(transport: Transport, size: int) -> None:
    transport.control(0x21, 1, 0, 0, struct.pack("<I", size))


def chunk(transport: Transport, index: int, data: bytes) -> None:
    if len(data) > EP0_DATA_OUT_MAX:
        raise ValueError(f"chunk {len(data)} > EP0_DATA_OUT_MAX {EP0_DATA_OUT_MAX}")
    transport.control(0x21, 1, index, 0, data)


def status(transport: Transport) -> int | None:
    """GETSTATUS. None when the IN stage is unusable.

    On this host IN control transfers stall on A7 devices (BugTracker #1169), so
    status is best-effort: the upload must not depend on it.
    """
    try:
        resp = transport.control(0xA1, 3, 0, 0, 6)
    except Exception:
        return None
    if len(resp) < 6:
        return None
    return resp[0]


def command(transport: Transport, line: str) -> None:
    transport.control(0x21, 3, 0, 0, line.encode() + b"\x00")


def upload(
    transport: Transport,
    data: bytes,
    chunk_size: int = 0x800,
    boot_commands: tuple[str, ...] = ("ramdisk", "xnu"),
) -> int:
    """Upload `data` over ep0 control transfers, then trigger the boot.

    Returns the number of bytes accepted by the device.
    """
    if chunk_size > EP0_DATA_OUT_MAX or chunk_size <= 0:
        raise ValueError(f"chunk_size must be in 1..{EP0_DATA_OUT_MAX}")

    resize(transport, len(data))

    accepted = 0
    for i in range(0, len(data), chunk_size):
        part = data[i : i + chunk_size]
        chunk(transport, i // chunk_size, part)
        st = status(transport)
        if st is not None and st != STATUS_OK:
            raise RuntimeError(f"device rejected chunk {i // chunk_size}")
        accepted += len(part)

    for line in boot_commands:
        command(transport, line)

    return accepted


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: {argv[0]} <ramdisk.dmg>", file=sys.stderr)
        return 2

    import usb.core  # noqa: PLC0415 - only needed on a real device

    dev = usb.core.find(idVendor=PONGO_VID, idProduct=PONGO_PID)
    if dev is None:
        print("pongoOS device 05ac:4141 not found (device not in download mode)", file=sys.stderr)
        return 1

    dev.set_timeout(10000)
    data = open(argv[1], "rb").read()
    received = upload(UsbTransport(dev), data)
    print(f"uploaded {len(data)} bytes, device holds {received} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Contract test for the forked pongoOS ep0 upload path.

FakePongo mirrors the forked handler semantics from
fork/pongoos-usbloader-ep0.patch: stock handlers for resize (0x21/1 wLength=4)
and stdin (0x21/3), plus the new DATA OUT chunk handler and GETSTATUS (0xA1/3).

The behaviour the fork changes is the interaction between the resize handler and
the data path: stock pongoOS arms a bulk OUT transfer on resize and only that
path fills the buffer. On this host the bulk transfer never completes, so
`waiting_bulk` stays set forever. The forked chunk handler must still accept data
while bulk is pending.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ep0_uploader import (
    EP0_DATA_OUT_MAX,
    STATUS_ERR_MANIFEST,
    STATUS_OK,
    chunk,
    command,
    resize,
    status,
    upload,
)


class Stall(Exception):
    """Control transfer rejected -> pongoOS returns false -> host stalls."""


class FakePongo:
    def __init__(self, buffer_size=0):
        self.buffer = bytearray()
        self.buffer_size = buffer_size
        self.waiting_bulk = False
        self.status = STATUS_OK
        self.shell = []
        self.bulk_completed = False  # bulk never completes on this host

    def control(self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None):
        if bmRequestType == 0x21 and bRequest == 1:
            if data_or_wLength == 0:
                self.buffer.clear()
                self.waiting_bulk = True
                return b""
            if isinstance(data_or_wLength, int):
                raise Stall("IN-style length on an OUT request")
            if len(data_or_wLength) == 4 and wValue == 0:  # stock resize handler
                newsz = int.from_bytes(data_or_wLength, "little")
                if newsz > self.buffer_size:
                    self.buffer_size = newsz
                self.buffer.clear()
                self.waiting_bulk = True
                return b""
            if len(data_or_wLength) > EP0_DATA_OUT_MAX:
                raise Stall("wLength exceeds ep0 DMA buffer")
            if len(self.buffer) + len(data_or_wLength) > self.buffer_size:
                self.status = STATUS_ERR_MANIFEST
                raise Stall("buffer overflow")
            self.buffer += data_or_wLength
            return b""
        if bmRequestType == 0x21 and bRequest == 3:
            self.shell.append(bytes(data_or_wLength).rstrip(b"\x00").decode())
            return b""
        if bmRequestType == 0xA1 and bRequest == 3:
            return bytes([self.status, 0, 0x5A, 0x04, 0, 0])
        raise Stall("unhandled request")


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


def main() -> int:
    payload = bytes(range(256)) * 40 + b"tail"
    t = FakePongo()
    accepted = upload(t, payload)

    ok = True
    ok &= check("data lands while bulk is pending and never completes",
                bytes(t.buffer) == payload and t.waiting_bulk and not t.bulk_completed)
    ok &= check("chunks arrive in order with sequential wValue", accepted == len(payload))
    ok &= check("finalize commands reach the shell", t.shell == ["ramdisk", "xnu"])

    # 4-byte control transfer must still go to the stock resize handler, not the buffer
    t2 = FakePongo()
    t2.control(0x21, 1, 0, 0, struct.pack("<I", 0x1000))
    ok &= check("resize path not swallowed by chunk handler",
                t2.buffer_size == 0x1000 and len(t2.buffer) == 0 and t2.waiting_bulk)

    # a 4-byte data chunk with a nonzero packet index must land in the buffer
    t5 = FakePongo()
    resize(t5, 64)
    chunk(t5, 1, b"abcd")
    ok &= check("4-byte chunk with nonzero wValue is data, not resize",
                bytes(t5.buffer) == b"abcd" and t5.buffer_size == 64)

    # oversized chunk rejected by the host guard
    t3 = FakePongo()
    try:
        chunk(t3, 0, b"x" * (EP0_DATA_OUT_MAX + 1))
        ok &= check("oversized chunk rejected", False)
    except ValueError:
        ok &= check("oversized chunk rejected", True)

    # overflow past the resized buffer must surface as errManifest, not silent truncation
    t4 = FakePongo()
    resize(t4, 64)
    chunk(t4, 0, b"x" * 64)
    try:
        chunk(t4, 1, b"x")
        ok &= check("overflow sets errManifest", False)
    except Stall:
        ok &= check("overflow sets errManifest", t4.status == STATUS_ERR_MANIFEST)

    # status must be readable over the forked GETSTATUS
    ok &= check("GETSTATUS readable", status(t4) == STATUS_ERR_MANIFEST and status(FakePongo()) == STATUS_OK)

    # IN stage is unusable on this host (BugTracker #1169): upload must still complete
    class NoIn(FakePongo):
        def control(self, bmRequestType, bRequest, wValue=0, wIndex=0, data_or_wLength=None):
            if bmRequestType & 0x80:
                raise Stall("IN control transfer stalls")
            return super().control(bmRequestType, bRequest, wValue, wIndex, data_or_wLength)

    t6 = NoIn()
    accepted6 = upload(t6, payload)
    ok &= check("upload completes when GETSTATUS stalls",
                bytes(t6.buffer) == payload and accepted6 == len(payload) and status(t6) is None)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

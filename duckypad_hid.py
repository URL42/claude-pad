#!/usr/bin/env python3
"""
duckypad_hid.py - talk to a duckyPad Pro over its Counted Buffer HID interface.

Protocol: https://github.com/duckyPad/duckyPad-Profile-Autoswitcher/blob/master/HID_details.md

Install:  pip3 install hidapi

Every call opens and closes the device, and on macOS opens it non-exclusively
(see _hid). An exclusive handle stops the pad's keypresses reaching the OS.
"""

import ctypes
import struct
import sys

VENDOR_ID = 0x0483
PID_DUCKYPAD_PRO = 0xD11D
COUNTED_BUFFER_USAGE = 0x3A
BUF_SIZE = 64

REPORT_ID = 0x05
CMD_QUERY_INFO = 0x00
CMD_SET_LED = 0x04
CMD_WRITE_GV = 0x19

STATUS_SUCCESS, STATUS_ERROR, STATUS_BUSY = 0, 1, 2

_hid_ready = False


class DuckyPadNotFound(Exception):
    pass


class DuckyPadBusy(Exception):
    """Pad is running a script or sitting in a menu; it rejected the command."""


class DuckyPadError(Exception):
    pass


# ---------- packet builders (pure, unit-tested) ----------

def build_packet(cmd, payload=b""):
    if len(payload) > BUF_SIZE - 3:
        raise ValueError("payload too long")
    buf = bytearray(BUF_SIZE)
    buf[0] = REPORT_ID
    buf[2] = cmd
    buf[3:3 + len(payload)] = payload
    return bytes(buf)


def build_set_led(index, r, g, b):
    if not 0 <= index <= 19:
        raise ValueError("LED index must be 0-19")
    return build_packet(CMD_SET_LED, bytes([index, r & 0xFF, g & 0xFF, b & 0xFF]))


def build_write_gv(values):
    """values: {gv_index: int}. Up to 12 per packet (5 bytes each)."""
    payload = bytearray()
    for idx, val in sorted(values.items()):
        if not 0 <= idx <= 31:
            raise ValueError("GV index must be 0-31")
        payload.append(idx | 0x80)
        payload += struct.pack("<I", val & 0xFFFFFFFF)
    return build_packet(CMD_WRITE_GV, bytes(payload))


# ---------- device I/O ----------

def _hid():
    """Import hidapi, and on macOS turn off its exclusive open.

    hidapi >= 0.12 seizes the device by default on macOS. Only root may seize a
    keyboard, so without this every open fails with "open failed" (the configurator
    gets away with it because it runs under sudo). The Python binding doesn't expose
    the switch, so call it in the already-loaded extension through ctypes.
    """
    global _hid_ready
    import hid
    if not _hid_ready:
        if sys.platform == "darwin":
            try:
                ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)
            except (OSError, AttributeError):
                pass  # older hidapi: no seize, nothing to turn off
        _hid_ready = True
    return hid


def _find_path():
    hid = _hid()
    for d in hid.enumerate(VENDOR_ID, PID_DUCKYPAD_PRO):
        if d.get("usage") == COUNTED_BUFFER_USAGE:
            return d["path"]
    raise DuckyPadNotFound("duckyPad Pro not found (plugged in? configurator closed?)")


def send(packet, timeout_ms=500):
    """Send one packet, return the 64-byte response. Raises Busy/Error/NotFound."""
    hid = _hid()
    dev = hid.device()
    dev.open_path(_find_path())
    try:
        dev.write(packet)
        resp = dev.read(BUF_SIZE, timeout_ms)
    finally:
        dev.close()
    if not resp:
        raise DuckyPadError("no response from pad")
    status = resp[2]
    if status == STATUS_BUSY:
        raise DuckyPadBusy()
    if status != STATUS_SUCCESS:
        raise DuckyPadError(f"pad returned status {status}")
    return resp


def set_led(index, r, g, b):
    send(build_set_led(index, r, g, b))


def write_gv(values):
    send(build_write_gv(values))


if __name__ == "__main__":
    # Quick hardware check: flash the top row red -> green -> off.
    import time
    for colour in [(255, 0, 0), (0, 255, 0), (0, 0, 0)]:
        for i in range(4):
            set_led(i, *colour)
        time.sleep(0.5)
    print("ok")

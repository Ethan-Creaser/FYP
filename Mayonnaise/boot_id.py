"""Generate and persist a unique boot ID across reboots.

Reads boot_id.bin to get the last used ID, picks a new random value in
1–254 that differs from it, writes the new value back, and returns it.

0x00 and 0xFF are reserved (0x00 = uninitialised sentinel, 0xFF = broadcast).
"""

import random

_BOOT_ID_FILE = "boot_id.bin"
_BOOT_ID_MIN  = 1
_BOOT_ID_MAX  = 254


def load_boot_id():
    """Return a new boot_id (1–254) guaranteed to differ from the last stored one."""
    last = _read_last()
    while True:
        new_id = random.randint(_BOOT_ID_MIN, _BOOT_ID_MAX)
        if new_id != last:
            break
    _write(new_id)
    print("BOOT_ID new={} last={}".format(new_id, last))
    return new_id


def _read_last():
    try:
        with open(_BOOT_ID_FILE, "rb") as f:
            data = f.read(1)
        return data[0] if data else 0
    except Exception:
        return 0


def _write(boot_id):
    with open(_BOOT_ID_FILE, "wb") as f:
        f.write(bytes([boot_id & 0xFF]))

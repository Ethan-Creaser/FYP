"""Identity helper: read/write a small identity.bin file used to set node IDs.

Format (v4): MAGIC (0xE9), node_id, uwb_id, neighbor_count, [n0, n1, ...], beacon_enabled, uwb_enabled
  neighbor_count = 0  → no restriction (accept all neighbors)
  neighbor_count > 0  → allowlist of that many node IDs follows
  beacon_enabled      → 1 (default) or 0; absent in older files → treated as 1
  uwb_enabled         → 1 (default) or 0; absent in older files → treated as 1

Old v3 format (no uwb_enabled byte) is read as uwb_enabled=True.
Old v2 format (no beacon_enabled byte) is read as beacon_enabled=True, uwb_enabled=True.
Old 3-byte format (no neighbor_count byte) is read as "no restriction".

Usage:
  from identity import get_ids, get_allowed_neighbors, get_beacon_enabled, get_uwb_enabled, write_identity
  node_id, uwb_id = get_ids()
  allowlist = get_allowed_neighbors()    # set or None (identity.bin only)
  write_identity(3, uwb_id=3, allowed_neighbors=[2, 4], beacon_enabled=True, uwb_enabled=True)
"""

MAGIC = 0xE9
DEFAULT_FILE = "identity.bin"


UWB_NONE = 0xFF   # sentinel stored in identity.bin meaning "no UWB attached"


def write_identity(node_id, uwb_id=None, allowed_neighbors=None, beacon_enabled=True, uwb_enabled=True, path=None):
    """Write identity file.

    allowed_neighbors: list/iterable of node IDs, or None/[] for no restriction.
    beacon_enabled: True (default) or False — persists across reboots.
    uwb_enabled: True (default) or False — holds UWB reset pin low on boot if False.
    uwb_id=None writes UWB_NONE (0xFF) — main.py will skip UWB initialisation.
    """
    stored_uwb = UWB_NONE if uwb_id is None else (int(uwb_id) & 0xFF)
    fn = path or DEFAULT_FILE
    buf = bytearray([MAGIC, int(node_id) & 0xFF, stored_uwb])
    if allowed_neighbors:
        nb = [int(n) & 0xFF for n in allowed_neighbors]
        buf.append(len(nb) & 0xFF)
        buf.extend(nb)
    else:
        buf.append(0)
    buf.append(1 if beacon_enabled else 0)
    buf.append(1 if uwb_enabled else 0)
    with open(fn, "wb") as f:
        f.write(bytes(buf))


def read_identity(path=None):
    """Return (node_id, uwb_id, allowed_neighbors_or_None, beacon_enabled, uwb_enabled) or None if missing/invalid.

    allowed_neighbors is a set of ints, or None if no restriction is stored.
    beacon_enabled defaults to True for files written before v3.
    uwb_enabled defaults to True for files written before v4.
    """
    fn = path or DEFAULT_FILE
    try:
        with open(fn, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if not data or len(data) < 3:
        return None
    if data[0] != MAGIC:
        return None
    node_id = int(data[1])
    uwb_id  = None if data[2] == UWB_NONE else int(data[2])
    neighbors = None
    count = 0
    if len(data) >= 4:
        count = int(data[3])
        if count > 0 and len(data) >= 4 + count:
            neighbors = set(int(data[4 + i]) for i in range(count))
    beacon_enabled = True
    beacon_byte_idx = 4 + count
    if len(data) > beacon_byte_idx:
        beacon_enabled = bool(data[beacon_byte_idx])
    uwb_enabled = True
    uwb_byte_idx = beacon_byte_idx + 1
    if len(data) > uwb_byte_idx:
        uwb_enabled = bool(data[uwb_byte_idx])
    return (node_id, uwb_id, neighbors, beacon_enabled, uwb_enabled)


def get_ids(cfg_path="config.json"):
    """Return (node_id, uwb_id). Prefer identity.bin, fallback to config.json, else (1,1)."""
    ids = read_identity()
    if ids:
        return (ids[0], ids[1])
    try:
        import json
        with open(cfg_path, "r") as f:
            cfg = json.load(f)
        node = int(cfg.get("node_id", 1))
        uwb = cfg.get("uwb_id")
        return (node, None if uwb is None else int(uwb))
    except Exception:
        return (1, 1)


def get_allowed_neighbors():
    """Return allowed_neighbors set from identity.bin, or None if not set."""
    ids = read_identity()
    if ids is not None and ids[2] is not None:
        return ids[2]
    return None


def get_beacon_enabled():
    """Return beacon_enabled from identity.bin, or True if not set."""
    ids = read_identity()
    if ids is not None:
        return ids[3]
    return True


def get_uwb_enabled():
    """Return uwb_enabled from identity.bin, or True if not set."""
    ids = read_identity()
    if ids is not None:
        return ids[4]
    return True

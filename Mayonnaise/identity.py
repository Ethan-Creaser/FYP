"""Identity helper: read/write a small identity.bin file used to set node IDs.

Format (v2): MAGIC (0xE9), node_id, uwb_id, neighbor_count, [n0, n1, ...]
  neighbor_count = 0  → no restriction (accept all neighbors)
  neighbor_count > 0  → allowlist of that many node IDs follows

Old 3-byte format (no neighbor_count byte) is read as "no restriction".

Usage:
  from identity import get_ids, get_allowed_neighbors, write_identity
  node_id, uwb_id = get_ids()
  allowlist = get_allowed_neighbors()    # set or None (identity.bin only)
  write_identity(3, uwb_id=3, allowed_neighbors=[2, 4])
"""

MAGIC = 0xE9
DEFAULT_FILE = "identity.bin"


def write_identity(node_id, uwb_id=None, allowed_neighbors=None, path=None):
    """Write identity file.

    allowed_neighbors: list/iterable of node IDs, or None/[] for no restriction.
    """
    if uwb_id is None:
        uwb_id = node_id
    fn = path or DEFAULT_FILE
    buf = bytearray([MAGIC, int(node_id) & 0xFF, int(uwb_id) & 0xFF])
    if allowed_neighbors:
        nb = [int(n) & 0xFF for n in allowed_neighbors]
        buf.append(len(nb) & 0xFF)
        buf.extend(nb)
    else:
        buf.append(0)
    with open(fn, "wb") as f:
        f.write(bytes(buf))


def read_identity(path=None):
    """Return (node_id, uwb_id, allowed_neighbors_or_None) or None if missing/invalid.

    allowed_neighbors is a set of ints, or None if no restriction is stored.
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
    uwb_id  = int(data[2])
    neighbors = None
    if len(data) >= 4:
        count = int(data[3])
        if count > 0 and len(data) >= 4 + count:
            neighbors = set(int(data[4 + i]) for i in range(count))
    return (node_id, uwb_id, neighbors)


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
        if uwb is None:
            uwb = node
        return (int(node), int(uwb))
    except Exception:
        return (1, 1)


def get_allowed_neighbors():
    """Return allowed_neighbors set from identity.bin, or None if not set."""
    ids = read_identity()
    if ids is not None and ids[2] is not None:
        return ids[2]
    return None

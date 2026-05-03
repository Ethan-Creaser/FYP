"""Identity helper: read/write a small identity.bin file used to set node IDs.

Format: 3 bytes: MAGIC (0xE9), node_id (1 byte), uwb_id (1 byte)

Usage:
  from identity import get_ids, write_identity, read_identity
  node_id, uwb_id = get_ids()
  write_identity(3, 3)
"""

MAGIC = 0xE9
DEFAULT_FILE = "identity.bin"


def write_identity(node_id, uwb_id=None, path=None):
    """Write identity file. uwb_id defaults to node_id if not provided."""
    if uwb_id is None:
        uwb_id = node_id
    fn = path or DEFAULT_FILE
    with open(fn, "wb") as f:
        f.write(bytes([MAGIC, int(node_id) & 0xFF, int(uwb_id) & 0xFF]))


def read_identity(path=None):
    """Return (node_id, uwb_id) or None if file missing/invalid."""
    fn = path or DEFAULT_FILE
    try:
        with open(fn, "rb") as f:
            data = f.read(3)
    except Exception:
        return None
    if not data or len(data) != 3:
        return None
    if data[0] != MAGIC:
        return None
    return (int(data[1]), int(data[2]))


def get_ids(cfg_path="config.json"):
    """Return (node_id, uwb_id). Prefer identity.bin, fallback to config.json, else (1,1)."""
    ids = read_identity()
    if ids:
        return ids
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

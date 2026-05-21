"""Set the permanent node identity for one egg.

Option 1 — edit the variables below and run this file:
    NODE_ID           = 3
    UWB_ID            = 3
    ALLOWED_NEIGHBORS = [2, 4]

Option 2 — call from the MicroPython REPL without editing:
    import hardcode_egg_id
    hardcode_egg_id.set_id(3)
    hardcode_egg_id.set_id(3, uwb_id=1, allowed_neighbors=[2, 4])

Set ALLOWED_NEIGHBORS = [] or None to use config.json allowed_neighbors instead
(or to allow all neighbors if config.json has none set).

Node ID assignments:
    1-14  mesh eggs
    99    ground station
    uwb_id must be 0-7 (only 8 UWB slots on BU03 hardware)
"""

# ── Edit these before running ─────────────────────────────────────────────────
NODE_ID           = 5
UWB_ID            = 5
ALLOWED_NEIGHBORS = [99]   # [] or None → fall back to config.json
# ─────────────────────────────────────────────────────────────────────────────

from identity import write_identity, read_identity


def set_id(node_id, uwb_id=None, allowed_neighbors=None):
    if uwb_id is None:
        uwb_id = node_id
    write_identity(node_id, uwb_id, allowed_neighbors=allowed_neighbors)
    data = read_identity()
    if data and data[0] == node_id and data[1] == uwb_id:
        nb = sorted(data[2]) if data[2] else []
        print("Identity set: node_id={} uwb_id={} allowed_neighbors={}".format(
            node_id, uwb_id, nb if nb else "(none — use config.json)"))
    else:
        print("VERIFY FAILED: read back {}".format(data))


def read():
    """Print what is currently stored in identity.bin."""
    data = read_identity()
    if data:
        nb = sorted(data[2]) if data[2] else []
        print("node_id={} uwb_id={} allowed_neighbors={}".format(
            data[0], data[1], nb if nb else "(none — use config.json)"))
    else:
        print("No identity.bin found")


if __name__ == "__main__":
    set_id(NODE_ID, UWB_ID, allowed_neighbors=ALLOWED_NEIGHBORS or None)

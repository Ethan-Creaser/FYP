"""Set the permanent node identity for one egg.

Option 1 — edit the variables below and run this file:
    NODE_ID = 3
    UWB_ID  = 3

Option 2 — call from the MicroPython REPL without editing:
    import hardcode_egg_id
    hardcode_egg_id.set_id(3)
    hardcode_egg_id.set_id(3, uwb_id=1)  # if uwb_id differs from node_id

Node ID assignments:
    1-14  mesh eggs
    99    ground station
    uwb_id must be 0-7 (only 8 UWB slots on BU03 hardware)
"""

# ── Edit these before running ─────────────────────────────────────────────────
NODE_ID = 2
UWB_ID  = 2
# ─────────────────────────────────────────────────────────────────────────────

from identity import write_identity, read_identity


def set_id(node_id, uwb_id=None):
    if uwb_id is None:
        uwb_id = node_id
    write_identity(node_id, uwb_id)
    data = read_identity()
    if data and data[0] == node_id and data[1] == uwb_id:
        print("Identity set: node_id={} uwb_id={}".format(node_id, uwb_id))
    else:
        print("VERIFY FAILED: read back {}".format(data))


def read():
    """Print what is currently stored in identity.bin."""
    data = read_identity()
    if data:
        print("node_id={} uwb_id={}".format(data[0], data[1]))
    else:
        print("No identity.bin found")


if __name__ == "__main__":
    set_id(NODE_ID, UWB_ID)

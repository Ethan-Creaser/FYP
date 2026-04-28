"""
main.py — Follower Node (IDs 1 … N)
=====================================
Upload this as main.py to every non-gateway board.
config.json must contain {"id": <unique integer >= 1>}

All logic lives in node.py, bu03.py, comms.py, localise.py.
This file just wires everything together and calls run().
"""

from bu03  import BU03
from comms import Comms
from node  import FollowerNode

def init_oled():
    try:
        from Drivers.oled.oled_class import OLED
        o = OLED(sda=8, scl=9)
        o.display_text("Booting...")
        print("[INIT] OLED OK")
        return o
    except Exception as e:
        print("[INIT] OLED FAILED:", e)
        return None

def main():
    import ujson
    cfg = {"id": 1, "name": "node"}
    try:
        with open("config.json") as f:
            cfg.update(ujson.load(f))
    except Exception as e:
        print("[CONFIG]", e)

    nid = int(cfg.get("id", 1))
    assert nid != 0, "Node 0 must use the gateway main.py!"
    print("[CONFIG] id={} name={}".format(nid, cfg.get("name", "node")))

    oled  = init_oled()
    comms = Comms()
    uwb   = BU03()

    FollowerNode(nid, comms, uwb, oled).run()

main()

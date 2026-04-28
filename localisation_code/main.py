"""
main.py — Unified Entry Point (all boards)
==========================================
Same file uploads to every board.
Role is determined entirely by config.json:

  {"id": 1,  "name": "node1"}   → Fixed node (self-organises)
  {"id": 2,  "name": "node2"}   → Fixed node (self-organises)
  {"id": 99, "name": "rover"}   → Rover (passive listener)

No fixed coordinator. Whichever node boots first in an area
becomes the local cluster leader. Clusters merge automatically.
"""

import ujson
from bu03  import BU03
from comms import Comms
from node  import Node


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
    print("=" * 48)
    cfg = {"id": 1, "name": "node"}
    try:
        with open("config.json") as f:
            cfg.update(ujson.load(f))
    except Exception as e:
        print("[CONFIG]", e)

    nid  = int(cfg.get("id", 1))
    name = cfg.get("name", "node{}".format(nid))
    print("[CONFIG] id={} name={}".format(nid, name))

    oled  = init_oled()
    comms = Comms()
    uwb   = BU03()

    Node(nid, name, comms, uwb, oled).run()


main()

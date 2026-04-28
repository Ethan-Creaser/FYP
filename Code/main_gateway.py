"""
main.py — Node 0 Gateway
=========================
Upload this as main.py to the Node 0 board.
config.json must contain {"id": 0}

All logic lives in gateway.py, bu03.py, comms.py.
This file just wires everything together and calls run().
"""

from bu03   import BU03
from comms  import Comms
from gateway import GatewayNode

def init_oled():
    try:
        from Drivers.oled.oled_class import OLED
        o = OLED(sda=8, scl=9)
        o.display_text("Gateway\nBooting...")
        print("[INIT] OLED OK")
        return o
    except Exception as e:
        print("[INIT] OLED FAILED:", e)
        return None

def main():
    import ujson
    cfg = {"id": 0}
    try:
        with open("config.json") as f:
            cfg.update(ujson.load(f))
    except Exception as e:
        print("[CONFIG]", e)

    assert int(cfg.get("id", -1)) == 0, "This main.py is for Node 0 only!"

    oled  = init_oled()
    comms = Comms()
    uwb   = BU03()

    GatewayNode(comms, uwb, oled).run()

main()

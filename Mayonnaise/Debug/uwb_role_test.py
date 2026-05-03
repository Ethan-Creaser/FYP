"""
uwb_role_test.py

Reads UWB slot ID and default role from identity.bin, configures the
BU03, then continuously prints distance readings over serial and BLE.

identity.bin layout (4 bytes):
  [0] 0xE9     magic
  [1] node_id
  [2] uwb_id   UWB slot 0-7
  [3] uwb_role 0=tag  1=anchor

Run hardcode_egg_id.py first to write the identity.
Connect with:  python3 PC_Scripts/bt_monitor.py --name egg_N
"""

import utime
from Drivers.uwb.bu03 import BU03
from Drivers.bt.bt_logger import BtLogger

CHANNEL = 1
RATE    = 1
FRAMES  = 20

# ------------------------------------------------------------------ #
# Read identity                                                        #
# ------------------------------------------------------------------ #

with open("identity.bin", "rb") as f:
    data = f.read(4)

if len(data) < 4 or data[0] != 0xE9:
    raise RuntimeError("identity.bin missing or corrupt — run hardcode_egg_id.py")

node_id  = data[1]
uwb_id   = data[2]
uwb_role = data[3]
role_str = "anchor" if uwb_role == 1 else "tag"

# ------------------------------------------------------------------ #
# Bluetooth                                                            #
# ------------------------------------------------------------------ #

node_name = "egg_{}".format(node_id)
try:
    bt = BtLogger(name=node_name)
    print("BT: advertising as {}".format(node_name))
except Exception as e:
    print("BT failed: {}".format(e))
    bt = None

def log(msg):
    print(msg)
    if bt is not None:
        bt.log(msg)

# ------------------------------------------------------------------ #
# Configure UWB                                                        #
# ------------------------------------------------------------------ #

log("Identity:  node_id={}  uwb_id={}  role={}".format(node_id, uwb_id, role_str))

uwb = BU03(
    data_uart_id=1, data_tx=17, data_rx=18,
    config_uart_id=2, config_tx=2, config_rx=1,
    reset_pin=15,
)

log("Configuring: slot={}  role={}  ch={}  rate={}".format(
    uwb_id, role_str, CHANNEL, RATE))
uwb.configure(uwb_id, uwb_role, channel=CHANNEL, rate=RATE)
log("Ready.")

# ------------------------------------------------------------------ #
# Read loop                                                            #
# ------------------------------------------------------------------ #

try:
    while True:
        if uwb_role == 1:
            log("  [anchor] holding...")
            utime.sleep_ms(1000)
        else:
            uwb.flush()
            raw = uwb.scan(frames=FRAMES)
            if raw:
                for slot, dist in sorted(raw.items()):
                    log("  slot {} -> {:.4f} m".format(slot, dist))
            else:
                log("  no data")
            utime.sleep_ms(500)

except KeyboardInterrupt:
    log("Stopped.")

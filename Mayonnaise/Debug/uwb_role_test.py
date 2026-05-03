"""
uwb_role_test.py

Reads UWB slot ID and default role from identity.bin, configures the
BU03, then continuously prints distance readings.

identity.bin layout (4 bytes):
  [0] 0xE9     magic
  [1] node_id
  [2] uwb_id   UWB slot 0-7
  [3] uwb_role 0=tag  1=anchor

Run hardcode_egg_id.py first to write the identity.
"""

import utime
from Drivers.uwb.bu03 import BU03

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

print("Identity:  node_id={}  uwb_id={}  role={}".format(node_id, uwb_id, role_str))

# ------------------------------------------------------------------ #
# Configure                                                            #
# ------------------------------------------------------------------ #

uwb = BU03(
    data_uart_id=1, data_tx=17, data_rx=18,
    config_uart_id=2, config_tx=2, config_rx=1,
    reset_pin=15,
)

print("Configuring: slot={}  role={}  ch={}  rate={}".format(
    uwb_id, role_str, CHANNEL, RATE))
uwb.configure(uwb_id, uwb_role, channel=CHANNEL, rate=RATE)
print("Ready.\n")

# ------------------------------------------------------------------ #
# Read loop                                                            #
# ------------------------------------------------------------------ #

try:
    while True:
        if uwb_role == 1:
            # Anchor — just stay alive, tag will range us
            print("  [anchor] holding...")
            utime.sleep_ms(1000)
        else:
            # Tag — scan and print distances
            uwb.flush()
            raw = uwb.scan(frames=FRAMES)
            if raw:
                for slot, dist in sorted(raw.items()):
                    print("  slot {} -> {:.4f} m".format(slot, dist))
            else:
                print("  no data")
            utime.sleep_ms(500)

except KeyboardInterrupt:
    print("Stopped.")

from Drivers.uwb.bu03 import BU03
import time

NODE_ID = 0
ROLE    = 0
CHANNEL = 1
RATE    = 1

uwb = BU03(
    data_uart_id=1, data_tx=17, data_rx=18,
    config_uart_id=2, config_tx=2, config_rx=1,
    reset_pin=15,
)

print("Configuring UWB as {} (node {})...".format("anchor" if ROLE else "tag", NODE_ID))
uwb.configure(NODE_ID, ROLE, channel=CHANNEL, rate=RATE)
print("Done. Reading distances...")

try:
    while True:
        distances = uwb.read_distance()
        print("Distances:",distances)
        if distances is not None:
            print("Distances:",distances)
            for slot, d in enumerate(distances):
                if d is not None:
                    print("Slot {}: {:.3f} m".format(slot, d))
        else:
            print("No frame")
        time.sleep_ms(100)
except KeyboardInterrupt:
    print("Stopped.")

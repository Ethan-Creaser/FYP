from Drivers.uwb.bu03 import BU03
import time

uwb = BU03(
    data_uart_id=1, data_tx=17, data_rx=18,
    config_uart_id=2, config_tx=2, config_rx=1,
    reset_pin=15,
)

try:
    while True:
        distances = uwb.read_distance()
        if distances is not None:
            for slot, d in enumerate(distances):
                if d is not None:
                    print("Slot {}: {:.3f} m".format(slot, d))
        else:
            print("No frame")
        time.sleep_ms(100)
except KeyboardInterrupt:
    print("Stopped.")


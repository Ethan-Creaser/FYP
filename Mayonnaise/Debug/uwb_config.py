from Drivers.uwb.bu03 import BU03

NODE_ID = 0
ROLE    = 1  # 0=tag, 1=anchor
CHANNEL = 1
RATE    = 1

try:
    uwb = BU03(
        data_uart_id=1, data_tx=17, data_rx=18,
        config_uart_id=2, config_tx=2, config_rx=1,
        reset_pin=15,
    )
    print("Configuring UWB as {} (node {})...".format("anchor" if ROLE else "tag", NODE_ID))
    uwb.configure(NODE_ID, ROLE, channel=CHANNEL, rate=RATE)
    print("Done.")
except KeyboardInterrupt:
    print("Interrupted")

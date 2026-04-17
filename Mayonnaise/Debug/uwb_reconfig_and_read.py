from Drivers.uwb.bu03 import BU03
import time

uwb = BU03(uart_id=1, tx=17, rx=18, config_uart_id=2, config_tx=2, config_rx=1,reset_pin=15)  # Use separate UART for config to avoid conflicts  

print("Configuring UWB...")
uwb.reconfigure(0,1,1,1)
uwb.reset(1000)

try:
    while True:
        distance = uwb.read_distance()
        print(f"Distance reading: {distance}")
        if distance is not None:
            print(f"Distance reading: {distance[0]} meters")
        time.sleep_us(100)
except KeyboardInterrupt:
    print("Stopped.")

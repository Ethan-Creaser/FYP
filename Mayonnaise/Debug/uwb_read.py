from Drivers.uwb.bu03 import BU03

uwb = BU03(uart_id=1, tx=17, rx=18, reset_pin=15)
import time 

try:
    while True:
        distance = uwb.read_distance()
        print(f"Distance reading: {distance}")
        if distance is not None:
            print(f"Distance reading: {distance[0]} meters")
        time.sleep_us(100)
except KeyboardInterrupt:
    print("Stopped.")


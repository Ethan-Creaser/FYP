from Drivers.uwb.bu03 import BU03
import time
from machine import Pin
try:
    uwb = BU03(uart_id=2, tx=6, rx=7)
    print("Configuring UWB...")
    uwb.configure(0,1,1,1)
except KeyboardInterrupt:
    print("Interrupted")
time.sleep(3)
reset_pin = Pin(15, Pin.OUT)
reset_pin.value(0)
time.sleep(0.5)
reset_pin.value(1)
time.sleep(2)

uwb = BU03(uart_id=1, tx=17, rx=18)
try:
    while True:
        distance = uwb.read_distance()
        print(f"Distance reading: {distance}")
        if distance is not None:
            print(f"Distance reading: {distance[0]} meters")
except KeyboardInterrupt:
    print("Stopped.")

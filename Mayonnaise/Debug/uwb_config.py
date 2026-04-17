from Drivers.uwb.bu03 import BU03
import time
from machine import Pin
try:
    uwb = BU03(uart_id=2, tx=2, rx=1)
    print("Configuring UWB...")
    uwb.configure(0,1,1,1)
except KeyboardInterrupt:
    print("Interrupted")

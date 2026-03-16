from machine import Pin
import time

reset_pin = Pin(2, Pin.OUT)
reset_pin.value(0)
time.sleep(0.5)
reset_pin.value(1)
time.sleep(0.5)
from machine import Pin
from neopixel import NeoPixel
import time

LED_PIN = 38      # ESP32-S3 onboard RGB (most DevKit boards)
NUM_PIXELS = 1

np = NeoPixel(Pin(LED_PIN, Pin.OUT), NUM_PIXELS)

while True:
    np[0] = (255, 0, 0)   # Red
    np.write()
    time.sleep(1)

    np[0] = (0, 255, 0)   # Green
    np.write()
    time.sleep(1)

    np[0] = (0, 0, 255)   # Blue
    np.write()
    time.sleep(1)
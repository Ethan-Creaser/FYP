from machine import Pin
from time import sleep
from neopixel import NeoPixel
pin = Pin(38, Pin.OUT)                          # Pin number for v1 of the above DevKitC, use pin 38 for v1.1
np = NeoPixel(pin, 1)                            # "1" = one RGB led on the "led bus"
while (True):
    np[0] = (255,0,0)
    np.write()
    sleep(1)
    np[0] = (0,255,0)
    np.write()
    sleep(1)
    np[0] = (0,0,255)
    np.write()
    sleep(1)
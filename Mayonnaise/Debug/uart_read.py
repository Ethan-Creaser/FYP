from machine import UART, Pin
import time

uart = UART(1, baudrate=115200, tx=Pin(17), rx=Pin(18))

try:
    while True:
        #if uart.any():
        data = uart.read()
        print("Raw:", data)
        time.sleep_ms(100)
except KeyboardInterrupt:
    print("Stopped.")

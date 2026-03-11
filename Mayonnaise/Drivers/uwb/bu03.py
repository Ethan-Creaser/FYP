# this file contains base and tag configuration for the uwb BU03 boards using UART
# list of AT commands https://core-electronics.com.au/attachments/uploads/bu03-at-commands.pdf
# BU03 documentation https://core-electronics.com.au/attachments/uploads/CE10222_bu03-kit_v1.1.0_specification.pdf
# Core Electronics Written and Video Guide https://core-electronics.com.au/guides/sensors/getting-started-with-ultra-wideband-and-measuring-distances-arduino-and-pico-guide/#configuring-the-bu03-boards
# author: Ashika
# last updated: 23/02/26

from machine import UART, Pin
import time

class BU03:
    def __init__(self, uart_id = 1, tx = 17, rx = 18):
        self.uart = UART(1, baudrate=115200, tx=17, rx=18)

    def send_at(self, cmd):
        self.uart.write(cmd + '\r\n')
        time.sleep(0.5)
        if self.uart.any():
            msg = self.uart.read()
            print(msg)

    def configure(self, id, role, channel = 1, rate = 1): # ID, Role (0 = tag, 1 = base station), Channel, Rate
        # uart.write('AT') can be used to test if AT commands are sending successfully, refer to docs for more info
        # send_at('AT+RESTORE') # factory reset
        self.send_at(f'AT+SETCFG={id},{role},{channel},{rate}') 
        self.send_at('AT+SAVE')
        self.send_at('AT+GETCFG')

    def verify_config(self, expected_id, expected_role):
        self.send_at('AT+GETCFG')

    
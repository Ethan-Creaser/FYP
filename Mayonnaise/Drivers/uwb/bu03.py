# this file contains base and tag configuration for the uwb BU03 boards using UART
# list of AT commands https://core-electronics.com.au/attachments/uploads/bu03-at-commands.pdf
# BU03 documentation https://core-electronics.com.au/attachments/uploads/CE10222_bu03-kit_v1.1.0_specification.pdf
# Core Electronics Written and Video Guide https://core-electronics.com.au/guides/sensors/getting-started-with-ultra-wideband-and-measuring-distances-arduino-and-pico-guide/#configuring-the-bu03-boards
# author: Ashika
# last updated: 23/02/26

import struct


from machine import UART, Pin
import time

class BU03:
    def __init__(self, uart_id=1, tx=17, rx=18):
        self.uart = UART(uart_id, baudrate=115200, tx=tx, rx=rx)

    def send_at(self, cmd):
        self.uart.write(cmd + '\r\n')
        time.sleep(0.5)
        if self.uart.any():
            msg = self.uart.read()
            print(msg)

    def configure(self, id, role, channel=1, rate=1):  # ID, Role (0 = tag, 1 = base station), Channel, Rate
        # uart.write('AT') can be used to test if AT commands are sending successfully, refer to docs for more info
        # send_at('AT+RESTORE') # factory reset
        self.send_at(f'AT+SETCFG={id},{role},{channel},{rate}')
        self.send_at('AT+SAVE')
        self.send_at('AT+GETCFG')

    def verify_config(self, expected_id, expected_role):
        self.send_at('AT+GETCFG')

    def read_distance(self, timeout_ms=200):
        """Wait up to timeout_ms for data, then decode. Returns distances list or None."""
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if self.uart.any():
                time.sleep_ms(20)  # let the full frame arrive
                message = self.uart.read()
                distances = self.decode_uwb_distances(message)
                if distances is not None:
                    return distances
        return None

    ### UWB Distance Decoding Functions

    def decode_uwb_distances(self, data):
        """
        Decode UWB distance data from binary message
        Returns list of distances in meters for each base station
        """
        if len(data) < 35:  # Minimum expected length
            return None

        # Check for header pattern
        if data[0:3] != b'\xaa%\x01':
            return None

        # Extract distance data (skip header, process 4-byte chunks)
        distances = []

        # Starting from byte 3, read 4-byte chunks for each base station
        for i in range(8):  # 8 base stations (0-7)
            byte_offset = 3 + (i * 4)  # Each distance is 4 bytes
            if byte_offset + 3 < len(data):
                # Read as little-endian 32-bit integer
                distance_raw = struct.unpack('<I', data[byte_offset:byte_offset+4])[0]
                # Convert to meters
                if distance_raw > 0:
                    distance_meters = distance_raw / 1000.0
                    distances.append(distance_meters)
                else:
                    distances.append(None)  # No signal/not visible
            else:
                distances.append(None)  # Base station not in data

        return distances

    def print_distances(self, distances):
        """Print distances in a readable format"""
        if distances is None:
            print("Invalid data received")
            return

        print("Base Station Distances:")
        for i, distance in enumerate(distances):
            if distance is not None and distance > 0:
                print(f"  BS{i}: {distance:.3f}m")
            else:
                print(f"  BS{i}: Not visible")
        print("-" * 30)


if __name__ == "__main__":
    uwb = BU03()
    uwb.configure(0, 1, 1, 1)  # ID=0, Role=Base Station, Channel=1, Rate=1
    while True:
        distances = uwb.read_distance()
        if distances:
            uwb.print_distances(distances)
        else:
            print("No distance data received")
        time.sleep(2)
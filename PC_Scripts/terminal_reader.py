#!/usr/bin/env python3
"""
ESP32 UART Monitor
Reads serial data from an ESP32 over USB and prints it to the terminal.

Usage:
    python3 esp32_monitor.py                          # Auto-detect port
    python3 esp32_monitor.py --port /dev/ttyUSB0      # Specify port
    python3 esp32_monitor.py --port COM3 --baud 9600  # Windows + custom baud
"""

import serial
import serial.tools.list_ports
import argparse
import sys
from datetime import datetime


def find_esp32_port():
    """Auto-detect the ESP32 serial port."""
    ports = serial.tools.list_ports.comports()

    # Known ESP32 USB-to-serial chip vendor/product strings
    esp_hints = ["cp210", "ch340", "ch9102", "ftdi", "esp32", "uart bridge"]

    for port in ports:
        desc = (port.description or "").lower()
        mfg  = (port.manufacturer or "").lower()
        print(f"Checking port: {port.device} — {port.description} (MFG: {port.manufacturer})")
        if any(hint in desc or hint in mfg for hint in esp_hints):
            print(f"[auto-detected] {port.device} — {port.description}")
            return port.device

    # Fall back: list all ports and let the user pick
    if ports:
        print("Could not auto-detect an ESP32. Available ports:")
        for i, p in enumerate(ports):
            print(f"  [{i}] {p.device} — {p.description}")
        choice = input("Enter index to use: ").strip()
        try:
            return ports[int(choice)].device
        except (ValueError, IndexError):
            pass

    print("No serial ports found. Check your USB connection.")
    sys.exit(1)


def monitor(port, baud, timestamps):
    print(f"Connecting to {port} at {baud} baud  (Ctrl+C to quit)\n")
    try:
        with serial.Serial(port, baud, timeout=1) as ser:
            ser.reset_input_buffer()
            while True:
                try:
                    line = ser.readline()
                    if line:
                        text = line.decode("utf-8", errors="replace").rstrip()
                        if timestamps:
                            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            print(f"[{ts}] {text}")
                        else:
                            print(text)
                except serial.SerialException as e:
                    print(f"\nSerial error: {e}")
                    break
    except serial.SerialException as e:
        print(f"Could not open {port}: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")


def main():
    parser = argparse.ArgumentParser(description="ESP32 UART monitor")
    parser.add_argument("--port",  "-p", help="Serial port (e.g. /dev/ttyUSB0 or COM3)")
    parser.add_argument("--baud",  "-b", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--timestamps", "-t", action="store_true", help="Prefix each line with a timestamp")
    args = parser.parse_args()

    port = args.port or find_esp32_port()
    monitor(port, args.baud, args.timestamps)


if __name__ == "__main__":
    main()
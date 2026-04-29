#!/usr/bin/env python3
"""
Egg Bluetooth Monitor
Streams live log output from an egg over BLE (Nordic UART Service).

No WiFi needed — works anywhere within Bluetooth range (~10 m).

Requirements:
    pip install bleak

Usage:
    python3 bt_monitor.py                        # scan and pick from list
    python3 bt_monitor.py --name egg_7           # auto-connect by name
    python3 bt_monitor.py --name egg_7 --timestamps
    python3 bt_monitor.py --name egg_7 --log run.log
    python3 bt_monitor.py --no-colour
"""

import argparse
import asyncio
import sys
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak is required:  pip install bleak")
    sys.exit(1)


# Nordic UART Service — TX characteristic (egg → PC, notify)
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

RECONNECT_DELAY = 3   # seconds between reconnect attempts


# ---------------------------------------------------------------------------
# ANSI colour helpers (identical to wifi_monitor.py)
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

FG_WHITE   = "\033[97m"
FG_YELLOW  = "\033[93m"
FG_GREEN   = "\033[92m"
FG_CYAN    = "\033[96m"
FG_BLUE    = "\033[94m"
FG_MAGENTA = "\033[95m"
FG_RED     = "\033[91m"
FG_GREY    = "\033[90m"


def _c(enabled, *codes):
    return "".join(codes) if enabled else ""


def colourise(line, colour):
    s = line.rstrip()
    if not colour:
        return s
    if s.startswith("-") and s.replace("-", "") == "":
        return _c(colour, DIM, FG_GREY) + s + RESET
    if s.startswith("=") and s.replace("=", "") == "":
        return _c(colour, DIM, FG_GREY) + s + RESET
    if s.startswith("["):
        if "[TX"    in s: return _c(colour, FG_BLUE)    + s + RESET
        if "[RX"    in s: return _c(colour, FG_MAGENTA) + s + RESET
        if "[RELAY" in s: return _c(colour, FG_CYAN)    + s + RESET
        return _c(colour, FG_CYAN) + s + RESET
    if s.startswith("Sent:"):     return _c(colour, DIM, FG_BLUE)    + s + RESET
    if s.startswith("Received:"): return _c(colour, DIM, FG_MAGENTA) + s + RESET
    if s.startswith("CAD:"):      return _c(colour, DIM, FG_GREY)    + s + RESET
    if s.startswith("  ") and ":" in s:
        colon = s.index(":")
        return (_c(colour, FG_GREEN) + s[:colon + 1] + RESET +
                _c(colour, FG_WHITE) + s[colon + 1:]  + RESET)
    if s and not s.startswith(" "):
        return _c(colour, BOLD, FG_YELLOW) + s + RESET
    return s


# ---------------------------------------------------------------------------
# BLE scanning
# ---------------------------------------------------------------------------

async def scan_for_egg(name, timeout=8.0):
    """Scan for a BLE device with the given name. Returns the device or None."""
    print("Scanning for '{}'…".format(name))
    device = await BleakScanner.find_device_by_name(name, timeout=timeout)
    return device


async def pick_egg_from_list():
    """Scan and let the user pick from discovered devices."""
    print("Scanning for BLE devices (5 s)…")
    devices = await BleakScanner.discover(timeout=5.0)
    if not devices:
        print("No BLE devices found.")
        return None

    eggs = [d for d in devices if d.name and "egg" in d.name.lower()]
    candidates = eggs if eggs else list(devices)

    print("\nFound {} device(s):".format(len(candidates)))
    for i, d in enumerate(candidates):
        print("  [{}] {} — {}".format(i, d.name or "(no name)", d.address))

    try:
        idx = int(input("Enter index to connect: ").strip())
        return candidates[idx]
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------

async def monitor(name, timestamps, colour, log_file):
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None

    # Partial-line buffer — BLE chunks can split across a \n boundary
    line_buf = ""

    def on_notify(_handle, data):
        nonlocal line_buf
        text = data.decode("utf-8", errors="replace")
        line_buf += text
        while "\n" in line_buf:
            raw, line_buf = line_buf.split("\n", 1)
            formatted = colourise(raw, colour)
            if timestamps:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                output = "[{}] {}".format(ts, formatted)
            else:
                output = formatted
            print(output)
            if log_fh:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                log_fh.write("[{}] {}\n".format(ts, raw))
                log_fh.flush()

    while True:
        try:
            # Find device
            if name:
                device = await scan_for_egg(name)
                if device is None:
                    print("{}'{}' not found — retrying in {}s…{}".format(
                        _c(colour, FG_RED), name, RECONNECT_DELAY, RESET))
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
            else:
                device = await pick_egg_from_list()
                if device is None:
                    sys.exit(1)
                name = device.name  # lock onto this device for reconnects

            print("{}Connecting to {} ({})…{}".format(
                _c(colour, FG_GREY), device.name, device.address, RESET))

            async with BleakClient(device) as client:
                print("{}Connected.  Ctrl+C to quit.{}".format(
                    _c(colour, BOLD, FG_YELLOW), RESET))
                await client.start_notify(NUS_TX_UUID, on_notify)
                # Keep running until the connection drops
                while client.is_connected:
                    await asyncio.sleep(0.5)

            print("{}Disconnected — reconnecting in {}s…{}".format(
                _c(colour, FG_GREY), RECONNECT_DELAY, RESET))
            await asyncio.sleep(RECONNECT_DELAY)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print("{}Error: {} — retrying in {}s…{}".format(
                _c(colour, FG_RED), exc, RECONNECT_DELAY, RESET))
            await asyncio.sleep(RECONNECT_DELAY)

    if log_fh:
        log_fh.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Egg Bluetooth log monitor")
    parser.add_argument(
        "--name", "-n", default=None,
        help="BLE device name to connect to (e.g. egg_7). "
             "Omit to scan and pick from a list.",
    )
    parser.add_argument(
        "--timestamps", "-t", action="store_true",
        help="Prefix each line with a local timestamp",
    )
    parser.add_argument(
        "--log", "-l", metavar="FILE",
        help="Also write output to a log file",
    )
    parser.add_argument(
        "--no-colour", action="store_true",
        help="Disable colour output",
    )
    args = parser.parse_args()

    colour = not args.no_colour and sys.stdout.isatty()

    try:
        asyncio.run(monitor(args.name, args.timestamps, colour, args.log))
    except KeyboardInterrupt:
        print("\n{}Monitor stopped.{}".format(_c(colour, FG_GREY), RESET))


if __name__ == "__main__":
    main()

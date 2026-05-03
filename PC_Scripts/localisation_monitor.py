#!/usr/bin/env python3
"""
Localisation Monitor
Connects to one or two eggs over BLE and logs only localisation-relevant
output to the console and an optional log file.

Requirements:
    pip install bleak

Usage:
    python3 localisation_monitor.py                         # scan and pick
    python3 localisation_monitor.py --names egg_6 egg_7     # two eggs
    python3 localisation_monitor.py --names egg_7           # one egg
    python3 localisation_monitor.py --names egg_6 egg_7 --log localisation_log.txt
    python3 localisation_monitor.py --no-colour
"""

import argparse
import asyncio
import sys
import threading
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak is required:  pip install bleak")
    sys.exit(1)


NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
RECONNECT_DELAY = 3

# Lines containing any of these are kept.
KEEP = [
    "LOCALISATION START",
    "LOCALISATION DONE",
    "LOCALISATION COORDINATOR",
    "LOCALISATION FOLLOWER",
    "LOCALISATION READY",
    "LOCALISATION RETRY",
    "LOCALISATION SOLVE",
    "LOCALISATION LIMIT",
    "DISC TRIGGERED",
    "DISC IGNORED",
    "LOCALISE TURN",
    "LOCALISE_TURN",
    "LOCALISE_RESULT",
    "LOCALISE_START",
    "LOCALISATION FOLLOWER",
    "Settle ms",
    "Peer",
    "Discovery",
    "UWB SCAN",
    "Localise result",
    "LOCALISE_POSITION",
    "REPAIR TRIGGER",
    "REPAIR REFRESH",
    "Coordinator:",
    "Members:",
    "UWB role:",
    "Reason:",
    "Raw:",
    "ERR",
    "STATUS",
    "State:",
    "Position:",
    "UWB CONFIG ERROR",
    "RANGE ERROR",
    "SOLVE ERROR",
    "setcfg",
    "GETCFG",
    "Role:",
    "solo node",
    "Tag:",
    "Window ms",
    "UWB ms",
]

# Lines matching these are always suppressed even if they hit a KEEP keyword.
SUPPRESS = [
    "[TX HEARTBEAT]",
    "[RX HEARTBEAT]",
    "RELAY HEARTBEAT",
    "Sent:",
    "Received:",
]


def should_log(line):
    for s in SUPPRESS:
        if s in line:
            return False
    for k in KEEP:
        if k in line:
            return True
    return False


# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

RESET      = "\033[0m"
BOLD       = "\033[1m"
DIM        = "\033[2m"
FG_WHITE   = "\033[97m"
FG_YELLOW  = "\033[93m"
FG_GREEN   = "\033[92m"
FG_CYAN    = "\033[96m"
FG_BLUE    = "\033[94m"
FG_MAGENTA = "\033[95m"
FG_RED     = "\033[91m"
FG_GREY    = "\033[90m"

EGG_COLOURS = [FG_CYAN, FG_MAGENTA, FG_YELLOW]


def _c(enabled, *codes):
    return "".join(codes) if enabled else ""


def colourise(line, colour, egg_colour):
    s = line.rstrip()
    if not colour:
        return s
    if s.startswith("-") and s.replace("-", "") == "":
        return _c(colour, DIM, FG_GREY) + s + RESET
    if s.startswith("  ") and ":" in s:
        colon = s.index(":")
        return (_c(colour, FG_GREEN) + s[:colon + 1] + RESET +
                _c(colour, FG_WHITE) + s[colon + 1:] + RESET)
    if s and not s.startswith(" "):
        return _c(colour, BOLD, egg_colour) + s + RESET
    return s


# ---------------------------------------------------------------------------
# BLE helpers
# ---------------------------------------------------------------------------

async def scan_for_egg(name, timeout=8.0):
    print("Scanning for '{}'…".format(name))
    return await BleakScanner.find_device_by_name(name, timeout=timeout)


async def pick_egg_from_list():
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
# Per-egg monitor task
# ---------------------------------------------------------------------------

async def monitor_egg(name, egg_colour, colour, log_fh, print_lock):
    line_buf = ""

    def on_notify(_handle, data):
        nonlocal line_buf
        text = data.decode("utf-8", errors="replace")
        line_buf += text
        while "\n" in line_buf:
            raw, line_buf = line_buf.split("\n", 1)
            stripped = raw.strip()
            if not stripped or not should_log(stripped):
                continue
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            tag = "[{}] [{}]".format(ts, name)
            formatted = colourise(stripped, colour, egg_colour)
            output = "{} {}".format(
                _c(colour, DIM, FG_GREY) + tag + RESET if colour else tag,
                formatted,
            )
            with print_lock:
                print(output)
                if log_fh:
                    log_fh.write("[{}] [{}] {}\n".format(ts, name, stripped))
                    log_fh.flush()

    while True:
        try:
            if name:
                device = await scan_for_egg(name)
                if device is None:
                    print("{}[{}] not found — retrying in {}s…{}".format(
                        _c(colour, FG_RED), name, RECONNECT_DELAY, RESET))
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
            else:
                device = await pick_egg_from_list()
                if device is None:
                    return
                name = device.name

            print("{}[{}] Connecting to {} ({})…{}".format(
                _c(colour, FG_GREY), name, device.name, device.address, RESET))

            async with BleakClient(device) as client:
                print("{}[{}] Connected.{}".format(
                    _c(colour, BOLD, egg_colour), name, RESET))
                await client.start_notify(NUS_TX_UUID, on_notify)
                while client.is_connected:
                    await asyncio.sleep(0.5)

            print("{}[{}] Disconnected — reconnecting in {}s…{}".format(
                _c(colour, FG_GREY), name, RECONNECT_DELAY, RESET))
            await asyncio.sleep(RECONNECT_DELAY)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print("{}[{}] Error: {} — retrying in {}s…{}".format(
                _c(colour, FG_RED), name, exc, RECONNECT_DELAY, RESET))
            await asyncio.sleep(RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(names, colour, log_file):
    log_fh = None
    if log_file:
        log_fh = open(log_file, "a", encoding="utf-8")
        log_fh.write("\n=== SESSION {} ===\n".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        log_fh.flush()

    print_lock = threading.Lock()

    tasks = []
    for i, name in enumerate(names):
        egg_colour = EGG_COLOURS[i % len(EGG_COLOURS)]
        tasks.append(asyncio.create_task(
            monitor_egg(name, egg_colour, colour, log_fh, print_lock)
        ))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
    finally:
        if log_fh:
            log_fh.close()


def main():
    parser = argparse.ArgumentParser(description="Localisation BLE monitor")
    parser.add_argument(
        "--names", "-n", nargs="+", default=None,
        metavar="NAME",
        help="BLE device name(s) to connect to, e.g. egg_6 egg_7. "
             "Omit to scan and pick.",
    )
    parser.add_argument(
        "--log", "-l", metavar="FILE", default="localisation_log.txt",
        help="Log file path (default: localisation_log.txt)",
    )
    parser.add_argument(
        "--no-colour", action="store_true",
        help="Disable colour output",
    )
    args = parser.parse_args()

    colour = not args.no_colour and sys.stdout.isatty()
    names = args.names if args.names else [None]

    print("Monitoring: {}".format(", ".join(n or "(pick)" for n in names)))
    print("Log file:   {}".format(args.log))
    print("Press Ctrl+C to stop.\n")

    try:
        asyncio.run(run(names, colour, args.log))
    except KeyboardInterrupt:
        print("\n{}Monitor stopped.{}".format(_c(colour, FG_GREY), RESET))


if __name__ == "__main__":
    main()

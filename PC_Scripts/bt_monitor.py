#!/usr/bin/env python3
"""
Egg Bluetooth Monitor
Streams live log output from one or more eggs over BLE (Nordic UART Service).

No WiFi needed — works anywhere within Bluetooth range (~10 m).

Requirements:
    pip install bleak

Usage:
    python3 bt_monitor.py                              # scan and pick from list
    python3 bt_monitor.py --name egg_7                 # single egg by name
    python3 bt_monitor.py --name egg_7 egg_6           # two eggs, colour-coded
    python3 bt_monitor.py --name egg_7 --timestamps
    python3 bt_monitor.py --name egg_7 --log run.log
    python3 bt_monitor.py --no-colour
    python3 bt_monitor.py --verbosity quiet            # only key events + errors
    python3 bt_monitor.py --verbosity errors           # only errors/warnings
    python3 bt_monitor.py --verbosity full             # everything including CAD/separators
    python3 bt_monitor.py --filter "TX|RX"             # only lines matching regex
"""

import argparse
import asyncio
import re
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

VERBOSITY_FULL   = "full"    # everything, including CAD lines and separators
VERBOSITY_NORMAL = "normal"  # default: all meaningful lines
VERBOSITY_QUIET  = "quiet"   # only tagged events, headers, and errors
VERBOSITY_ERRORS = "errors"  # only error / warning lines


# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

FG_WHITE   = "\033[97m"
FG_YELLOW  = "\033[93m"
FG_ORANGE  = "\033[33m"
FG_GREEN   = "\033[92m"
FG_TEAL    = "\033[36m"
FG_CYAN    = "\033[96m"
FG_BLUE    = "\033[94m"
FG_MAGENTA = "\033[95m"
FG_PINK    = "\033[35m"
FG_RED     = "\033[91m"
FG_GREY    = "\033[90m"

# Distinct prefix colours — supports up to 8 eggs simultaneously
EGG_PREFIX_COLOURS = [
    BOLD + FG_CYAN,
    BOLD + FG_YELLOW,
    BOLD + FG_GREEN,
    BOLD + FG_MAGENTA,
    BOLD + FG_ORANGE,
    BOLD + FG_RED,
    BOLD + FG_PINK,
    BOLD + FG_TEAL,
]


def _c(enabled, *codes):
    return "".join(codes) if enabled else ""


def colourise(line, colour):
    s = line.rstrip()
    if not colour:
        return s

    # Separator lines
    if s.startswith("-") and s.replace("-", "") == "":
        return _c(colour, DIM, FG_GREY) + s + RESET
    if s.startswith("=") and s.replace("=", "") == "":
        return _c(colour, DIM, FG_GREY) + s + RESET

    # Tagged log lines  [TX …] [RX …] [RELAY …]
    if s.startswith("["):
        if "[TX"    in s: return _c(colour, FG_BLUE)           + s + RESET
        if "[RX"    in s: return _c(colour, FG_MAGENTA)        + s + RESET
        if "[RELAY" in s: return _c(colour, FG_CYAN)           + s + RESET
        return _c(colour, FG_CYAN) + s + RESET

    # Prefixed log lines
    if s.startswith("Sent:"):     return _c(colour, DIM, FG_BLUE)    + s + RESET
    if s.startswith("Received:"): return _c(colour, DIM, FG_MAGENTA) + s + RESET
    if s.startswith("CAD:"):      return _c(colour, DIM, FG_GREY)    + s + RESET

    # Severity keywords (case-insensitive check on upper)
    su = s.upper()
    if any(k in su for k in ("FATAL", "EXCEPTION", "TRACEBACK")):
        return _c(colour, BOLD, FG_RED)     + s + RESET
    if "ERROR" in su:
        return _c(colour, BOLD, FG_RED)     + s + RESET
    if "WARN" in su:
        return _c(colour, BOLD, FG_YELLOW)  + s + RESET
    if "INFO" in su and s.startswith("INFO"):
        return _c(colour, FG_GREEN)         + s + RESET
    if "DEBUG" in su and s.startswith("DEBUG"):
        return _c(colour, FG_GREY)          + s + RESET
    if "OK" in su and s.startswith("OK"):
        return _c(colour, BOLD, FG_GREEN)   + s + RESET

    # Indented key: value lines
    if s.startswith("  ") and ":" in s:
        colon = s.index(":")
        return (_c(colour, FG_GREEN) + s[:colon + 1] + RESET +
                _c(colour, FG_WHITE) + s[colon + 1:]  + RESET)

    # Top-level section headers / unindented lines
    if s and not s.startswith(" "):
        return _c(colour, BOLD, FG_YELLOW) + s + RESET

    return s


# ---------------------------------------------------------------------------
# Verbosity filtering
# ---------------------------------------------------------------------------

def should_show(raw, verbosity, filter_pat):
    """Return True if this line should be printed given current verbosity/filter."""
    if filter_pat and not filter_pat.search(raw):
        return False

    if verbosity == VERBOSITY_FULL:
        return True

    if verbosity == VERBOSITY_ERRORS:
        su = raw.upper()
        return any(k in su for k in ("ERROR", "WARN", "EXCEPTION", "FATAL", "TRACEBACK"))

    if verbosity == VERBOSITY_QUIET:
        s = raw.strip()
        # Only show lines that are tagged TX / RX / RELAY — no drops, no noise
        if not s.startswith("["):
            return False
        if "[DROP" in s:
            return False
        return "[TX" in s or "[RX" in s or "[RELAY" in s

    # VERBOSITY_NORMAL — hide only blank/empty lines in stream; show everything else
    return True


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

async def monitor(name, timestamps, colour, log_file, verbosity, filter_pat,
                  prefix_colour=None):
    """Monitor a single egg. prefix_colour is used when monitoring multiple eggs."""
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    line_buf = ""

    def make_prefix(label):
        if not colour or not prefix_colour:
            return "[{}] ".format(label)
        return prefix_colour + "[{}]".format(label) + RESET + " "

    def on_notify(_handle, data):
        nonlocal line_buf
        text = data.decode("utf-8", errors="replace")
        line_buf += text
        while "\n" in line_buf:
            raw, line_buf = line_buf.split("\n", 1)
            if not should_show(raw, verbosity, filter_pat):
                continue
            formatted = colourise(raw, colour)
            if prefix_colour:
                formatted = make_prefix(name) + formatted
            if timestamps:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                output = "[{}] {}".format(ts, formatted)
            else:
                output = formatted
            print(output)
            if log_fh:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                log_fh.write("[{}] [{}] {}\n".format(ts, name, raw))
                log_fh.flush()

    while True:
        try:
            if name:
                device = await scan_for_egg(name)
                if device is None:
                    print("{}{}'{}' not found — retrying in {}s…{}".format(
                        make_prefix(name) if prefix_colour else "",
                        _c(colour, FG_RED), name, RECONNECT_DELAY, RESET))
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
            else:
                device = await pick_egg_from_list()
                if device is None:
                    sys.exit(1)
                name = device.name

            print("{}{}Connecting to {} ({})…{}".format(
                make_prefix(name) if prefix_colour else "",
                _c(colour, FG_GREY), device.name, device.address, RESET))

            async with BleakClient(device) as client:
                print("{}{}Connected.  Ctrl+C to quit.{}".format(
                    make_prefix(name) if prefix_colour else "",
                    _c(colour, BOLD, FG_GREEN), RESET))
                await client.start_notify(NUS_TX_UUID, on_notify)
                while client.is_connected:
                    await asyncio.sleep(0.5)

            print("{}{}Disconnected — reconnecting in {}s…{}".format(
                make_prefix(name) if prefix_colour else "",
                _c(colour, FG_GREY), RECONNECT_DELAY, RESET))
            await asyncio.sleep(RECONNECT_DELAY)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print("{}{}Error: {} — retrying in {}s…{}".format(
                make_prefix(name) if prefix_colour else "",
                _c(colour, FG_RED), exc, RECONNECT_DELAY, RESET))
            await asyncio.sleep(RECONNECT_DELAY)

    if log_fh:
        log_fh.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Egg Bluetooth log monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
verbosity levels:
  full    show everything (CAD noise, separator lines, blank lines)
  normal  default — all meaningful lines
  quiet   only [TX] / [RX] / [RELAY] events — no drops, no noise
  errors  only lines that contain ERROR / WARN / EXCEPTION / FATAL
        """,
    )
    parser.add_argument(
        "--name", "-n", nargs="*", default=None, metavar="NAME",
        help="BLE device name(s) to connect to (e.g. egg_7, or egg_7 egg_6 for two eggs). "
             "Omit to scan and pick from a list.",
    )
    parser.add_argument(
        "--timestamps", "-t", action="store_true",
        help="Prefix each line with a local timestamp",
    )
    parser.add_argument(
        "--log", "-l", metavar="FILE",
        help="Also write output to a log file (always full verbosity in file)",
    )
    parser.add_argument(
        "--no-colour", action="store_true",
        help="Disable colour output",
    )
    parser.add_argument(
        "--verbosity", "-v",
        choices=[VERBOSITY_FULL, VERBOSITY_NORMAL, VERBOSITY_QUIET, VERBOSITY_ERRORS],
        default=VERBOSITY_NORMAL,
        metavar="LEVEL",
        help="How much to print: full | normal (default) | quiet | errors",
    )
    parser.add_argument(
        "--filter", "-f", metavar="PATTERN",
        help="Only show lines matching this regex pattern (applied before colouring)",
    )
    args = parser.parse_args()

    colour = not args.no_colour and sys.stdout.isatty()

    filter_pat = None
    if args.filter:
        try:
            filter_pat = re.compile(args.filter)
        except re.error as e:
            print("Invalid --filter pattern: {}".format(e))
            sys.exit(1)

    names = args.name or []
    multi = len(names) > 1

    async def run():
        kwargs = dict(
            timestamps=args.timestamps,
            colour=colour,
            log_file=args.log,
            verbosity=args.verbosity,
            filter_pat=filter_pat,
        )
        if not names:
            await monitor(None, **kwargs)
        elif not multi:
            await monitor(names[0], **kwargs)
        else:
            tasks = [
                asyncio.create_task(monitor(
                    name,
                    prefix_colour=EGG_PREFIX_COLOURS[i % len(EGG_PREFIX_COLOURS)],
                    **kwargs,
                ))
                for i, name in enumerate(names)
            ]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n{}Monitor stopped.{}".format(_c(colour, FG_GREY), RESET))


if __name__ == "__main__":
    main()

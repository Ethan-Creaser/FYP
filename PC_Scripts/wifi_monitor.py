#!/usr/bin/env python3
"""
Egg WiFi Monitor
Streams live log output from an egg's WiFi interface over HTTP/SSE.

The egg must have wifi_enabled=true in config.json and be running main.py.
Connect your PC to the egg's WiFi AP first, then run this script.

Usage:
    python3 wifi_monitor.py                        # default 192.168.4.1:80
    python3 wifi_monitor.py --ip 192.168.4.1
    python3 wifi_monitor.py --ip 192.168.4.1 --port 80
    python3 wifi_monitor.py --timestamps
    python3 wifi_monitor.py --log egg6.log
    python3 wifi_monitor.py --no-colour
"""

import argparse
import sys
import time
import urllib.request
from datetime import datetime


# ---------------------------------------------------------------------------
# ANSI colour helpers
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
    stripped = line.rstrip()
    if not colour:
        return stripped

    # Separator lines  (--- ... ---)
    if stripped.startswith("-") and stripped.replace("-", "") == "":
        return _c(colour, DIM, FG_GREY) + stripped + RESET

    # Section headers printed by main / camera boot
    if stripped.startswith("=") and stripped.replace("=", "") == "":
        return _c(colour, DIM, FG_GREY) + stripped + RESET

    # Compact packet lines  [TX HEARTBEAT] ...  [RX ...] ...  [RELAY ...] ...
    if stripped.startswith("["):
        if "[TX" in stripped:
            return _c(colour, FG_BLUE) + stripped + RESET
        if "[RX" in stripped:
            return _c(colour, FG_MAGENTA) + stripped + RESET
        if "[RELAY" in stripped:
            return _c(colour, FG_CYAN) + stripped + RESET
        return _c(colour, FG_CYAN) + stripped + RESET

    # Low-level send / receive prints from the radio driver
    if stripped.startswith("Sent:"):
        return _c(colour, DIM, FG_BLUE) + stripped + RESET
    if stripped.startswith("Received:"):
        return _c(colour, DIM, FG_MAGENTA) + stripped + RESET
    if stripped.startswith("CAD:"):
        return _c(colour, DIM, FG_GREY) + stripped + RESET

    # Key–value item lines (two leading spaces)
    if stripped.startswith("  ") and ":" in stripped:
        colon = stripped.index(":")
        label = stripped[:colon + 1]
        value = stripped[colon + 1:]
        return (
            _c(colour, FG_GREEN) + label + RESET +
            _c(colour, FG_WHITE) + value + RESET
        )

    # Event title lines (text between separators — non-empty, no leading spaces)
    if stripped and not stripped.startswith(" "):
        return _c(colour, BOLD, FG_YELLOW) + stripped + RESET

    return stripped


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

def sse_lines(url, timeout=10):
    """Generator that yields raw SSE data lines from the given URL."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        },
    )
    response = urllib.request.urlopen(req, timeout=timeout)
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            yield line[5:].lstrip(" ")


def check_reachable(ip, port, colour):
    """Quick TCP ping to give a clear error if the Mac isn't on the egg's WiFi."""
    import socket
    try:
        s = socket.create_connection((ip, port), timeout=3)
        s.close()
        return True
    except OSError:
        print("{}Cannot reach {}:{} — make sure your Mac is connected to the egg's WiFi AP.{}".format(
            _c(colour, FG_RED), ip, port, RESET))
        return False


def monitor(ip, port, timestamps, colour, log_file):
    url = "http://{}:{}/events".format(ip, port)
    base_url = "http://{}:{}".format(ip, port)

    print("{}Egg WiFi Monitor{}  {}{}{}".format(
        _c(colour, BOLD, FG_YELLOW), RESET,
        _c(colour, DIM, FG_GREY), base_url, RESET,
    ))
    print("{}Ctrl+C to quit{}".format(_c(colour, DIM, FG_GREY), RESET))
    print()

    if not check_reachable(ip, port, colour):
        sys.exit(1)

    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    retry_delay = 2

    try:
        while True:
            try:
                print("{}Connecting to {}…{}".format(
                    _c(colour, FG_GREY), url, RESET))
                for data_line in sse_lines(url):
                    retry_delay = 2  # reset on successful data
                    formatted = colourise(data_line, colour)

                    if timestamps:
                        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        output = "[{}] {}".format(ts, formatted)
                    else:
                        output = formatted

                    print(output)

                    if log_fh:
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                        log_fh.write("[{}] {}\n".format(ts, data_line))
                        log_fh.flush()

                print("{}Connection closed.{}".format(_c(colour, FG_GREY), RESET))

            except KeyboardInterrupt:
                raise

            except Exception as exc:
                print("{}Error: {}  — retrying in {}s…{}".format(
                    _c(colour, FG_RED), exc, retry_delay, RESET))

            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

    except KeyboardInterrupt:
        print("\n{}Monitor stopped.{}".format(_c(colour, FG_GREY), RESET))
    finally:
        if log_fh:
            log_fh.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Egg WiFi log monitor")
    parser.add_argument(
        "--ip", "-i", default="192.168.4.1",
        help="Egg IP address (default: 192.168.4.1)",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=80,
        help="HTTP port (default: 80)",
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
        "--no-colour", "-n", action="store_true",
        help="Disable colour output",
    )
    args = parser.parse_args()

    colour = not args.no_colour and sys.stdout.isatty()
    monitor(args.ip, args.port, args.timestamps, colour, args.log)


if __name__ == "__main__":
    main()

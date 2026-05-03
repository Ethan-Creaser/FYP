"""
Localisation logger — run on PC.

Reads serial from up to two egg nodes simultaneously and writes filtered
localisation events to log.txt with timestamps.

Usage:
    python localisation_logger.py             # auto-detect ports
    python localisation_logger.py COM3        # one port
    python localisation_logger.py COM3 COM4   # two ports (two eggs)

Requires: pip install pyserial
"""

import sys
import time
import threading
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

BAUD = 115200
LOG_FILE = "localisation_log.txt"

# Lines containing any of these keywords are logged.
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
    "Discovery",
    "UWB SCAN",
    "Localise result",
    "LOCALISE_POSITION",
    "Position ",
    "REPAIR TRIGGER",
    "REPAIR REFRESH",
    "LOCALISATION",
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
    "AT+SAVE",
    "setcfg",
    "GETCFG",
    "Role:",
]

# Suppress lines matching these (too noisy even if they hit a KEEP keyword).
SUPPRESS = [
    "[TX HEARTBEAT]",
    "[RX HEARTBEAT]",
    "RELAY HEARTBEAT",
    "Sent:",
    "Received:",
]

lock = threading.Lock()
log_file = None


def should_log(line):
    for s in SUPPRESS:
        if s in line:
            return False
    for k in KEEP:
        if k in line:
            return True
    return False


def write(tag, line):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    entry = "[{}] [{}] {}".format(ts, tag, line.strip())
    with lock:
        print(entry)
        if log_file:
            log_file.write(entry + "\n")
            log_file.flush()


def read_port(port, tag):
    try:
        ser = serial.Serial(port, BAUD, timeout=1)
        write(tag, "=== connected to {} ===".format(port))
    except Exception as e:
        print("ERROR: could not open {}: {}".format(port, e))
        return

    blank_run = 0  # consecutive blank/noise lines — track section boundaries
    in_section = False

    try:
        while True:
            try:
                raw = ser.readline()
            except Exception as e:
                write(tag, "READ ERROR: {}".format(e))
                break

            if not raw:
                continue

            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue

            stripped = line.strip()
            if not stripped:
                continue

            # Section dividers (---) mark the start of a log block — log
            # them so the file retains the structure.
            if stripped.startswith("---"):
                in_section = True
                if should_log(stripped) or blank_run == 0:
                    write(tag, stripped)
                blank_run = 0
                continue

            if should_log(stripped):
                write(tag, stripped)
                blank_run = 0
            else:
                blank_run += 1

    finally:
        ser.close()


def auto_detect_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        print("No serial ports found.")
        sys.exit(1)
    if len(ports) > 2:
        print("Found ports: {}".format(ports))
        print("Using first two. Pass ports explicitly to override.")
        ports = ports[:2]
    return ports


def main():
    global log_file

    if len(sys.argv) >= 2:
        ports = sys.argv[1:]
    else:
        ports = auto_detect_ports()

    print("Logging ports: {}".format(ports))
    print("Writing to: {}".format(LOG_FILE))
    print("Press Ctrl-C to stop.\n")

    tags = ["EGG_A", "EGG_B"] if len(ports) > 1 else ["EGG"]

    log_file = open(LOG_FILE, "a")
    log_file.write("\n=== SESSION {} ===\n".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    log_file.flush()

    threads = []
    for port, tag in zip(ports, tags):
        t = threading.Thread(target=read_port, args=(port, tag), daemon=True)
        t.start()
        threads.append(t)

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if log_file:
            log_file.close()


if __name__ == "__main__":
    main()

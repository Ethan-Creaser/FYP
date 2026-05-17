"""Beacon Control — enable or disable beaconing on eggs via direct BLE sweep.

Scans for and connects to each egg individually by its BLE name, sends a
0xD2 command directly (no mesh forwarding), waits for BEACON_OK, then moves
to the next egg.  The change is written to identity.bin on the egg so it
persists across reboots.

Requires: pip install bleak

Usage:
    python beacon_control.py --eggs 1-8 --disable
    python beacon_control.py --eggs 1,5,8 --enable
    python beacon_control.py --eggs 1,3,5-8 --disable
"""

import argparse
import asyncio
import sys
import time as _time

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("bleak not installed — run: pip install bleak")
    sys.exit(1)

NUS_RX_UUID  = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_UUID  = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

CMD_BEACON   = 0xD2
SCAN_TIMEOUT = 10.0
ACK_TIMEOUT  = 10.0


def parse_eggs(spec):
    ids = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ids.extend(range(int(lo), int(hi) + 1))
        else:
            ids.append(int(part))
    return sorted(set(ids))


async def apply_one(egg_id, enable):
    name   = "egg_{}".format(egg_id)
    action = "ENABLE" if enable else "DISABLE"

    print("\n── {} — {} beacon ──".format(name, action))
    print("  Scanning…")
    device = await BleakScanner.find_device_by_name(name, timeout=SCAN_TIMEOUT)
    if device is None:
        print("  ✗ not found (timed out after {}s)".format(SCAN_TIMEOUT))
        return False

    confirmed = False
    buf = ""

    def on_notify(_handle, data):
        nonlocal buf, confirmed
        buf += data.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip()
            if line:
                print("  [egg]", line)
            if line.startswith("BEACON_OK"):
                confirmed = True

    try:
        async with BleakClient(device) as client:
            if not client.is_connected:
                print("  ✗ failed to connect")
                return False

            print("  Connected.")
            await client.start_notify(NUS_TX_UUID, on_notify)

            payload = bytes([CMD_BEACON, egg_id & 0xFF, 1 if enable else 0])
            await client.write_gatt_char(NUS_RX_UUID, payload, response=True)

            deadline = _time.monotonic() + ACK_TIMEOUT
            while _time.monotonic() < deadline:
                await asyncio.sleep(0.3)
                if confirmed:
                    break

            await client.stop_notify(NUS_TX_UUID)
    except Exception as exc:
        print("  ✗ error: {}".format(exc))
        return False

    if confirmed:
        print("  ✓ beacon {}D".format(action.lower()[:-1]))
    else:
        print("  ✗ no confirmation (timed out after {}s)".format(ACK_TIMEOUT))
    return confirmed


async def run(egg_ids, enable):
    results = {}
    for egg_id in egg_ids:
        results[egg_id] = await apply_one(egg_id, enable)

    action = "ENABLE" if enable else "DISABLE"
    ok  = [i for i, r in results.items() if r]
    bad = [i for i, r in results.items() if not r]

    print("\n=== Summary ({}) ===".format(action))
    if ok:
        print("  ✓ eggs: {}".format(ok))
    if bad:
        print("  ✗ eggs: {}".format(bad))


def main():
    parser = argparse.ArgumentParser(
        description="Enable or disable beaconing on eggs via direct BLE sweep.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python beacon_control.py --eggs 1-8 --disable
  python beacon_control.py --eggs 1,5,8 --enable
  python beacon_control.py --eggs 1,3,5-8 --disable
        """,
    )
    parser.add_argument("--eggs", required=True, metavar="SPEC",
                        help="Egg range/list — e.g. '1-8', '1,5,8', '1,3,5-8'")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable",  action="store_true", help="Enable beaconing")
    group.add_argument("--disable", action="store_true", help="Disable beaconing")
    args = parser.parse_args()

    try:
        egg_ids = parse_eggs(args.eggs)
    except ValueError:
        print("ERROR: invalid --eggs spec '{}'".format(args.eggs))
        sys.exit(1)

    action = "ENABLE" if args.enable else "DISABLE"
    print("=== Beacon Control ===")
    print("Eggs   : {}".format(egg_ids))
    print("Action : {}".format(action))

    asyncio.run(run(egg_ids, args.enable))


if __name__ == "__main__":
    main()

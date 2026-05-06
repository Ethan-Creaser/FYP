"""Cycle through a range of target eggs over one BLE session and collect UWB distances.

Each egg is temporarily set to uwb_id=0 role=0 (tag) so it can range against all
active anchors.  After scan results are received the gateway egg sends a restore
command (0xD0) over LoRa; the target egg receives it and reverts to its own
identity.bin uwb_id with role=1 (anchor).

Usage:
    python auto_uwb_scan.py --name egg_6 --range 1 5
    python auto_uwb_scan.py --name egg_6 --targets 1 2 3 7

Output:
    uwb_scan.csv  (appended if it already exists)

Requires:
    pip install bleak
"""

import argparse
import asyncio
import csv
import os
import sys
import time as _time

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("bleak not installed — run: pip install bleak")
    sys.exit(1)

NUS_RX_UUID   = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_UUID   = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

CMD_UWB         = 0xCF   # [CMD_UWB, target_id, uwb_id, role]
CMD_UWB_RESTORE = 0xD0   # [CMD_UWB_RESTORE, target_id]

SCAN_TIMEOUT  = 10.0
# Time to wait after sending the restore command before moving to the next egg.
# The egg runs configure_warm (~5.5 s) on receipt; add headroom for LoRa round trip.
RESTORE_WAIT  = 8.0

_CSV_PATH   = "uwb_scan.csv"
_CSV_HEADER = ["pc_timestamp_ms", "node_id", "uwb_id", "role", "slot", "distance_m"]


def _parse_uwb_result(line):
    # UWB_RESULT node=7 uwb_id=0 role=0 slot=1 dist=0.6300
    try:
        parts = {}
        for token in line.split()[1:]:
            k, v = token.split("=")
            parts[k] = v
        ts = int(_time.time() * 1000)
        return [ts, int(parts["node"]), int(parts["uwb_id"]),
                int(parts["role"]), int(parts["slot"]), float(parts["dist"])]
    except Exception:
        return None


def _write_csv(rows):
    if not rows:
        return
    write_header = not os.path.exists(_CSV_PATH) or os.path.getsize(_CSV_PATH) == 0
    with open(_CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(_CSV_HEADER)
        w.writerows(rows)
    print("Logged {} row(s) to {}".format(len(rows), _CSV_PATH))


async def run_scan(via_name, target_ids, per_egg_timeout):
    print("\nScanning for '{}'...".format(via_name))
    device = await BleakScanner.find_device_by_name(via_name, timeout=SCAN_TIMEOUT)
    if device is None:
        print("ERROR: '{}' not found.".format(via_name))
        return []

    print("Found {} ({})".format(device.name, device.address))

    all_rows   = []
    notify_buf = ""
    collecting = False
    scan_rows  = []

    def on_notify(sender, data):
        nonlocal notify_buf
        try:
            notify_buf += data.decode("utf-8")
        except Exception:
            return
        while "\n" in notify_buf:
            line, notify_buf = notify_buf.split("\n", 1)
            line = line.rstrip()
            if not line:
                continue
            print("[egg]", line)
            if collecting and line.startswith("UWB_RESULT "):
                row = _parse_uwb_result(line)
                if row:
                    scan_rows.append(row)

    async with BleakClient(device) as client:
        if not client.is_connected:
            print("ERROR: failed to connect")
            return []

        print("Connected.\n")
        await client.start_notify(NUS_TX_UUID, on_notify)

        for idx, node_id in enumerate(target_ids):
            print("── [{}/{}] egg_{} ──".format(idx + 1, len(target_ids), node_id))

            # ── Phase 1: configure as tag and collect scan results ──────────────
            print("  [scan]    configuring as tag (uwb_id=0, role=0)...")
            scan_rows.clear()
            collecting = True

            payload = bytes([CMD_UWB, node_id & 0xFF, 0, 0])
            await client.write_gatt_char(NUS_RX_UUID, payload, response=True)

            deadline  = _time.monotonic() + per_egg_timeout
            got_first = False
            tail_end  = None

            while _time.monotonic() < deadline:
                await asyncio.sleep(0.5)

                if not got_first:
                    for row in scan_rows:
                        if row[1] == node_id:
                            got_first = True
                            tail_end  = _time.monotonic() + 3.0
                            print("  [scan]    first result received — 3 s tail window...")
                            break

                if got_first and tail_end and _time.monotonic() >= tail_end:
                    break

            collecting = False
            rows_this  = [r for r in scan_rows if r[1] == node_id]

            if rows_this:
                print("  [scan]    {} slot(s) received.".format(len(rows_this)))
                all_rows.extend(rows_this)
            else:
                print("  [scan]    no results (timed out).")

            # ── Phase 2: send restore command — egg reverts to identity.bin id ──
            print("  [restore] sending restore command to egg_{}...".format(node_id))
            restore_payload = bytes([CMD_UWB_RESTORE, node_id & 0xFF])
            await client.write_gatt_char(NUS_RX_UUID, restore_payload, response=True)

            # Wait for LoRa delivery + egg's configure_warm to complete
            print("  [restore] waiting {}s for egg to restore...\n".format(RESTORE_WAIT))
            await asyncio.sleep(RESTORE_WAIT)

        await client.stop_notify(NUS_TX_UUID)

    print("Session complete. {} total measurements.".format(len(all_rows)))
    return all_rows


def main():
    parser = argparse.ArgumentParser(
        description="Cycle eggs as tag, collect UWB distances, then restore via command.")
    parser.add_argument("--name", required=True,
                        help="BLE name of gateway egg (e.g. egg_6)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--range", nargs=2, type=int, metavar=("START", "END"),
                       help="Inclusive range of target egg IDs, e.g. --range 1 10")
    group.add_argument("--targets", nargs="+", type=int, metavar="ID",
                       help="Explicit list of target egg IDs")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Seconds to wait per egg for scan result (default 30)")
    args = parser.parse_args()

    target_ids = (list(range(args.range[0], args.range[1] + 1))
                  if args.range else args.targets)

    print("=== Auto UWB Scan ===")
    print("Gateway : {}".format(args.name))
    print("Targets : {}".format(target_ids))
    print("Timeout : {}s per egg   Restore wait: {}s".format(args.timeout, RESTORE_WAIT))
    print()
    for nid in target_ids:
        print("  egg_{:<3}  scan→ uwb_id=0 role=0 (tag)   restore→ identity.bin default (anchor)".format(nid))
    print()

    confirm = input("Start scan? [y/N] : ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        sys.exit(0)

    rows = asyncio.run(run_scan(args.name, target_ids, args.timeout))
    _write_csv(rows)


if __name__ == "__main__":
    main()

"""Range Test Logger — Test 1 (RSSI & RTT vs Distance)

Connects to an egg over BLE and records RSSI, SNR, and RTT at each
distance step.  The operator moves the second egg to the next position
and presses Enter; all samples collected since the last step are saved
under that distance label.

Connect to the SENDER egg for:
  - RSSI of return path (ACK packets)   -- [radio] RX kind=ACK ... rssi=X snr=Y
  - RTT                                 -- [N] ACK confirmed seq=X rtt_ms=Y

Connect to the RECEIVER egg for:
  - RSSI of forward path (DATA packets) -- [radio] RX kind=DATA ... rssi=X snr=Y
  (RTT not available from receiver side)

Requires: pip install bleak

Usage:
    python range_test_logger.py --name egg_6 --start 0.0 --step 0.5
    python range_test_logger.py --name egg_6 --start 0.0 --step 0.5 --samples 15

Output:
    range_test_results/range_YYYYMMDD_HHMMSS.csv
    Columns: distance_m, rssi_dbm, snr_db, rtt_ms, kind, pc_timestamp_ms
"""

import argparse
import asyncio
import csv
import datetime
import os
import re
import sys
import threading
import time as _time

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("bleak not installed — run: pip install bleak")
    sys.exit(1)

NUS_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

_OUT_DIR    = "range_test_results"
_CSV_HEADER = ["distance_m", "rssi_dbm", "snr_db", "rtt_ms", "kind", "pc_timestamp_ms"]

# [radio] RX kind=DATA src=1 dst=2 sender=1 seq=5 len=20 rssi=-75 snr=9
_RX_RE  = re.compile(r'\[radio\] RX kind=(\w+).*rssi=(-?\d+)\s+snr=(-?\d+)')

# [1] ACK confirmed seq=5 rtt_ms=150
_ACK_RE = re.compile(r'\[\d+\] ACK confirmed seq=\d+ rtt_ms=(\d+)')


def _make_csv_path():
    os.makedirs(_OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(_OUT_DIR, "range_{}.csv".format(stamp))


def _write_csv(rows, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_CSV_HEADER)
        w.writerows(rows)
    print("\nSaved {} sample(s) to {}".format(len(rows), path))


# ── BLE collection thread ────────────────────────────────────────────────────

class Collector:
    """Thread-safe sample accumulator shared between asyncio and the main loop."""

    def __init__(self):
        self._lock    = threading.Lock()
        self._pending = []   # samples not yet assigned to a distance step
        self.running  = True

    def add(self, kind, rssi, snr, rtt_ms=None):
        ts = int(_time.time() * 1000)
        with self._lock:
            self._pending.append((kind, rssi, snr, rtt_ms, ts))

    def flush(self):
        """Return and clear all pending samples."""
        with self._lock:
            out, self._pending = self._pending, []
        return out


async def _monitor(name, collector):
    """Async BLE monitor — runs in a background thread via asyncio.run()."""
    RECONNECT = 3.0
    buf       = ""

    def on_notify(_handle, data):
        nonlocal buf
        buf += data.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip()

            m = _RX_RE.search(line)
            if m and m.group(1) in ("DATA", "ACK"):
                kind = m.group(1)
                rssi = int(m.group(2))
                snr  = int(m.group(3))
                collector.add(kind, rssi, snr)
                return

            m = _ACK_RE.search(line)
            if m:
                rtt = int(m.group(1))
                # Pair with the most recent ACK-kind RX entry if present
                # (RTT line always follows the [radio] RX line for the same seq)
                with collector._lock:
                    for i in range(len(collector._pending) - 1, -1, -1):
                        entry = list(collector._pending[i])
                        if entry[0] == "ACK" and entry[3] is None:
                            entry[3] = rtt
                            collector._pending[i] = tuple(entry)
                            break
                    else:
                        # No matching RX line — store as standalone RTT entry
                        collector._pending.append(
                            ("ACK", None, None, rtt, int(_time.time() * 1000))
                        )

    while collector.running:
        try:
            print("Scanning for '{}'…".format(name))
            device = await BleakScanner.find_device_by_name(name, timeout=8.0)
            if device is None:
                print("'{}' not found — retrying in {}s…".format(name, RECONNECT))
                await asyncio.sleep(RECONNECT)
                continue

            print("Connected to {} ({}).\n".format(device.name, device.address))
            async with BleakClient(device) as client:
                await client.start_notify(NUS_TX_UUID, on_notify)
                while client.is_connected and collector.running:
                    await asyncio.sleep(0.25)
            print("Disconnected — reconnecting…")
            await asyncio.sleep(RECONNECT)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print("BLE error: {} — retrying in {}s…".format(exc, RECONNECT))
            await asyncio.sleep(RECONNECT)


def _ble_thread(name, collector):
    asyncio.run(_monitor(name, collector))


# ── Main operator loop ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Range test logger — record RSSI/SNR/RTT at each distance step.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow:
  1. Place both eggs at the starting position.
  2. Run this script and wait for 'Connected' message.
  3. Press Enter to mark each position and move the mobile egg one step further.
  4. Press Ctrl+C or type 'q' + Enter to end the session.

Tip: Connect to the SENDER egg for RTT + reverse-path RSSI.
     Connect to the RECEIVER egg for forward-path RSSI (most meaningful).
        """,
    )
    parser.add_argument("--name",    required=True,  help="BLE name of the egg to monitor (e.g. egg_6)")
    parser.add_argument("--start",   type=float, default=0.0,  help="Starting distance in metres (default 0.0)")
    parser.add_argument("--step",    type=float, default=0.5,  help="Distance increment per step in metres (default 0.5)")
    parser.add_argument("--samples", type=int,   default=None, help="Minimum samples before auto-advancing (optional; default: manual Enter)")
    args = parser.parse_args()

    collector    = Collector()
    csv_path     = _make_csv_path()
    current_dist = args.start
    all_rows     = []

    # Start BLE in a background thread
    t = threading.Thread(target=_ble_thread, args=(args.name, collector), daemon=True)
    t.start()

    print("=== Range Test Logger ===")
    print("Egg     : {}".format(args.name))
    print("Start   : {:.2f} m    Step: {:.2f} m".format(args.start, args.step))
    print("Output  : {}".format(csv_path))
    print()
    print("Waiting for BLE connection…  (Ctrl+C to quit)")
    print()

    try:
        while True:
            prompt = "  [dist={:.2f}m] Press Enter to record this position (or 'q' to quit): ".format(current_dist)
            try:
                inp = input(prompt)
            except EOFError:
                break

            if inp.strip().lower() == "q":
                break

            # Flush samples collected since last step
            samples = collector.flush()

            if not samples:
                print("  ⚠  No samples received yet — is the egg sending packets?")
                continue

            # Build rows for this distance step
            step_rows = []
            for kind, rssi, snr, rtt_ms, ts in samples:
                if rssi is None and rtt_ms is None:
                    continue  # skip empty entries
                row = [
                    round(current_dist, 3),
                    rssi if rssi is not None else "",
                    snr  if snr  is not None else "",
                    rtt_ms if rtt_ms is not None else "",
                    kind,
                    ts,
                ]
                step_rows.append(row)
                all_rows.append(row)

            if not step_rows:
                print("  ⚠  Samples collected but none had valid RSSI or RTT — skipping.")
                continue

            # Summary for this step
            rssies = [r[1] for r in step_rows if r[1] != ""]
            rtts   = [r[3] for r in step_rows if r[3] != ""]
            snrs   = [r[2] for r in step_rows if r[2] != ""]

            rssi_str = "{:.1f} dBm (n={})".format(
                sum(rssies) / len(rssies), len(rssies)) if rssies else "—"
            snr_str  = "{:.1f} dB".format(
                sum(snrs) / len(snrs)) if snrs else "—"
            rtt_str  = "{:.0f} ms (n={})".format(
                sum(rtts) / len(rtts), len(rtts)) if rtts else "—"

            print("  → {:.2f} m | RSSI: {} | SNR: {} | RTT: {}".format(
                current_dist, rssi_str, snr_str, rtt_str))

            current_dist = round(current_dist + args.step, 3)

    except KeyboardInterrupt:
        print("\nInterrupted.")

    finally:
        collector.running = False
        _write_csv(all_rows, csv_path)
        print("Done.")


if __name__ == "__main__":
    main()

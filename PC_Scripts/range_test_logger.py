"""Range Test Logger — Test 1 (RSSI & RTT vs Distance)

Connects to the gateway egg over BLE.  At each distance step the operator
presses Enter; the script sends a 0xD3 ping command to trigger exactly
--samples packets from the gateway to the target egg.  The script then waits
for the PING_DONE confirmation, collects RSSI/SNR/RTT from the ACK stream,
and saves results before advancing to the next step.

This requires no changes to config.json — hw_test_enabled is NOT needed.

Requires: pip install bleak

Usage:
    python range_test_logger.py --name egg_6 --target 7
    python range_test_logger.py --name egg_6 --target 7 --samples 15 --step 1.0

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

from topology import Topology
from topology_check import TopologyCheck

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("bleak not installed — run: pip install bleak")
    sys.exit(1)

NUS_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

CMD_PING = 0xD3

_OUT_DIR    = "range_test_results"
_CSV_HEADER = ["distance_m", "rssi_dbm", "snr_db", "rtt_ms", "kind", "pc_timestamp_ms"]

# [radio] RX kind=ACK src=2 dst=1 sender=2 seq=5 len=9 rssi=-75 snr=9
_RX_RE  = re.compile(r'\[radio\] RX kind=(\w+).*rssi=(-?\d+)\s+snr=(-?\d+(?:\.\d+)?)')

# [radio] TX kind=DATA src=6 dst=8 sender=6 seq=1 len=15
_TX_RE  = re.compile(r'\[radio\] TX kind=DATA.*seq=(\d+)')

# [1] ACK confirmed seq=5 from=2 dst=7 rtt_ms=143
_ACK_RE = re.compile(r'\[\d+\] ACK confirmed seq=(\d+).*rtt_ms=(\d+)')

_START_RE = re.compile(r'^PING_START')
_DONE_RE  = re.compile(r'^PING_DONE(?:\s+first_seq=(\d+)\s+last_seq=(\d+))?')


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


# ── Thread-safe state ────────────────────────────────────────────────────────

class Collector:
    def __init__(self, n_samples):
        self._lock        = threading.Lock()
        self._samples     = []       # samples for the current ping burst
        self._collecting  = False    # True between PING_START and PING_DONE
        self._done        = threading.Event()
        self._grace_timer = None
        self._valid_first = None     # seq range from PING_DONE — used to drop stale ACKs
        self._valid_last  = None
        self.n_samples   = n_samples
        self.running     = True
        # Set by the BLE thread once connected
        self._loop   = None
        self._client = None

    def set_ble(self, loop, client):
        with self._lock:
            self._loop   = loop
            self._client = client

    def clear_ble(self):
        with self._lock:
            self._loop   = None
            self._client = None

    @property
    def connected(self):
        with self._lock:
            return self._client is not None

    def trigger_ping(self, target_id):
        """Send the 0xD3 ping command from the main thread into the asyncio loop."""
        with self._lock:
            loop   = self._loop
            client = self._client
        if loop is None or client is None:
            return False
        payload = bytes([CMD_PING, target_id & 0xFF, self.n_samples & 0xFF])

        async def _send():
            await client.write_gatt_char(NUS_RX_UUID, payload, response=True)

        future = asyncio.run_coroutine_threadsafe(_send(), loop)
        try:
            future.result(timeout=5.0)
            return True
        except Exception as e:
            print("  Ping command error: {}".format(e))
            return False

    def on_ping_start(self):
        with self._lock:
            self._samples.clear()
            self._valid_first = None
            self._valid_last  = None
            self._collecting  = True
            self._done.clear()
            if self._grace_timer:
                self._grace_timer.cancel()
            self._grace_timer = None

    def on_ping_done(self, first_seq=None, last_seq=None):
        # Record the valid seq range then drop any samples outside it (stale ACKs).
        with self._lock:
            self._valid_first = first_seq
            self._valid_last  = last_seq
            if first_seq is not None and last_seq is not None:
                self._samples = [s for s in self._samples
                                 if s[5] is None or first_seq <= s[5] <= last_seq]
            if self._grace_timer:
                self._grace_timer.cancel()
            t = threading.Timer(2.0, self._grace_expire)
            self._grace_timer = t
        t.start()

    def _grace_expire(self):
        with self._lock:
            self._collecting = False
            self._grace_timer = None
        self._done.set()

    def add_rx(self, kind, rssi, snr):
        ts = int(_time.time() * 1000)
        with self._lock:
            if self._collecting and kind == "ACK":
                # seq slot (index 5) filled later by fill_rtt
                self._samples.append([kind, rssi, snr, None, ts, None])

    def fill_rtt(self, seq, rtt_ms):
        """Back-fill seq and RTT into the most recent ACK sample that lacks them."""
        with self._lock:
            for i in range(len(self._samples) - 1, -1, -1):
                if self._samples[i][0] == "ACK" and self._samples[i][3] is None:
                    self._samples[i][3] = rtt_ms
                    self._samples[i][5] = seq
                    break

    def wait(self, timeout=60):
        return self._done.wait(timeout=timeout)

    def get_samples(self):
        with self._lock:
            return [tuple(s[:5]) for s in self._samples]   # drop internal seq field


# ── BLE monitor (runs in background thread) ──────────────────────────────────

async def _monitor(name, collector):
    RECONNECT = 3.0
    buf       = ""

    def on_notify(_handle, data):
        nonlocal buf
        buf += data.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip()
            if not line:
                continue

            if _START_RE.match(line):
                collector.on_ping_start()
                return

            m = _DONE_RE.match(line)
            if m:
                first = int(m.group(1)) if m.group(1) else None
                last  = int(m.group(2)) if m.group(2) else None
                collector.on_ping_done(first_seq=first, last_seq=last)
                return

            m = _TX_RE.search(line)
            if m:
                print("send  seq={}".format(m.group(1)))
                return

            m = _RX_RE.search(line)
            if m and m.group(1) == "ACK":
                print("ACK   rssi={}dBm  snr={}dB".format(m.group(2), m.group(3)))
                collector.add_rx(m.group(1), int(m.group(2)), float(m.group(3)))
                return

            m = _ACK_RE.search(line)
            if m:
                print("      rtt={}ms  seq={}".format(m.group(2), m.group(1)))
                collector.fill_rtt(int(m.group(1)), int(m.group(2)))

    while collector.running:
        try:
            device = await BleakScanner.find_device_by_name(name, timeout=8.0)
            if device is None:
                await asyncio.sleep(RECONNECT)
                continue

            print("Connected to {} ({}).\n".format(device.name, device.address))
            async with BleakClient(device) as client:
                collector.set_ble(asyncio.get_event_loop(), client)
                await client.start_notify(NUS_TX_UUID, on_notify)
                while client.is_connected and collector.running:
                    await asyncio.sleep(0.1)
                collector.clear_ble()

            if collector.running:
                print("Disconnected — reconnecting in {}s…".format(RECONNECT))
            await asyncio.sleep(RECONNECT)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            collector.clear_ble()
            print("BLE error: {} — retrying in {}s…".format(exc, RECONNECT))
            await asyncio.sleep(RECONNECT)


def _ble_thread(name, collector):
    asyncio.run(_monitor(name, collector))


# ── Main operator loop ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Range test logger — PC-triggered RSSI/RTT measurement per distance step.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow:
  1. Place both eggs at the starting position.
  2. Run this script and wait for 'Connected' message.
  3. Press Enter at each position — the script fires --samples pings and records results.
  4. Move the mobile egg one step further, repeat.
  5. Press Ctrl+C or type 'q' + Enter to end the session.

Note: the gateway egg (--name) fires the pings; --target is the receiver.
      No config.json changes needed.
        """,
    )
    parser.add_argument("--name",     required=True,        help="BLE name of gateway egg (e.g. egg_6)")
    parser.add_argument("--target",   required=True, type=int, help="Node ID of receiver egg (e.g. 7)")
    parser.add_argument("--start",    type=float, default=0.0, help="Starting distance in metres (default 0.0)")
    parser.add_argument("--step",     type=float, default=0.5, help="Distance increment per step in metres (default 0.5)")
    parser.add_argument("--samples",  type=int,   default=10,  help="Pings per distance step (default 10)")
    parser.add_argument("--topology", metavar="FILE",
                        help="Topology JSON to verify via egg_99 before starting (e.g. topology.json)")
    args = parser.parse_args()

    # Wait-for-ACK mode: each ping waits up to HOP_ACK_TIMEOUT * MAX_HOP_RETRIES (15 s)
    # before giving up, plus a 10 s routing buffer for the first RREQ round-trip.
    ping_timeout = args.samples * 15 + 10

    if args.topology:
        print("=== Topology Check ===")
        expected = Topology.load(args.topology).as_expected()

        async def _check():
            async with TopologyCheck.connect() as topo:
                return await topo.verify(expected)

        if not asyncio.run(_check()):
            print("Topology check FAILED — fix the network before running range test.")
            sys.exit(1)
        print("Topology check passed — proceeding.\n")

    collector = Collector(n_samples=args.samples)
    csv_path  = _make_csv_path()
    dist      = args.start
    all_rows  = []

    t = threading.Thread(target=_ble_thread, args=(args.name, collector), daemon=True)
    t.start()

    print("=== Range Test Logger ===")
    print("Gateway  : {}  →  Target: egg_{}".format(args.name, args.target))
    print("Start    : {:.2f} m    Step: {:.2f} m    Samples: {}".format(
        args.start, args.step, args.samples))
    print("Output   : {}".format(csv_path))
    print()
    print("Scanning for '{}'…  (Ctrl+C to quit)".format(args.name))
    print()

    try:
        while not collector.connected:
            _time.sleep(0.5)

        try:
            input("  Connected — press Enter to start, or Ctrl+C to quit: ")
        except EOFError:
            pass

        while True:
            prompt = "  [dist={:.2f}m] Press Enter to fire {} pings (or 'q' to quit): ".format(
                dist, args.samples)
            try:
                inp = input(prompt)
            except EOFError:
                break

            if inp.strip().lower() == "q":
                break

            if not collector.connected:
                print("  ⚠  Not connected — waiting for BLE link")
                while not collector.connected:
                    _time.sleep(0.5)
                print("  Reconnected.")
                continue

            print("  Firing {} ping(s) at egg_{}…".format(args.samples, args.target))
            ok = collector.trigger_ping(args.target)
            if not ok:
                print("  ⚠  Failed to send ping command")
                continue

            collector.on_ping_start()  # reset state before wait() — BLE version arrives late
            print("PING_START")

            if not collector.wait(timeout=ping_timeout):
                print("  ⚠  Timed out waiting for PING_DONE — using partial results")
                collector.on_ping_done()   # unblock for next step

            print("PING_DONE")

            samples = collector.get_samples()
            if not samples:
                print("  ⚠  No samples received")
                continue

            # Build CSV rows for this step
            step_rows = []
            for kind, rssi, snr, rtt_ms, ts in samples:
                row = [round(dist, 3), rssi, snr,
                       rtt_ms if rtt_ms is not None else "",
                       kind, ts]
                step_rows.append(row)
                all_rows.append(row)

            # Live summary
            rssies = [r[1] for r in step_rows if r[1] is not None]
            snrs   = [r[2] for r in step_rows if r[2] is not None]
            rtts   = [r[3] for r in step_rows if r[3] != ""]

            rssi_str = "{:.1f} dBm (n={})".format(
                sum(rssies) / len(rssies), len(rssies)) if rssies else "—"
            snr_str  = "{:.1f} dB".format(
                sum(snrs) / len(snrs)) if snrs else "—"
            rtt_str  = "{:.0f} ms (n={})".format(
                sum(rtts) / len(rtts), len(rtts)) if rtts else "—"

            print("  → {:.2f} m | RSSI: {} | SNR: {} | RTT: {}".format(
                dist, rssi_str, snr_str, rtt_str))

            _write_csv(all_rows, csv_path)

            dist = round(dist + args.step, 3)

    except KeyboardInterrupt:
        print("\nInterrupted.")

    finally:
        collector.running = False
        _write_csv(all_rows, csv_path)
        print("Done.")


if __name__ == "__main__":
    main()

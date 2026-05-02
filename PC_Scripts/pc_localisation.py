#!/usr/bin/env python3
"""
PC-Centralised Localisation
============================
Demonstrates an alternative localisation architecture where eggs report raw
UWB distance measurements to a PC (via BLE → LoRa mesh hop), and the PC
runs MDS to solve positions.

Key differences from on-device approach:
  - No coordinator election needed on the eggs
  - Eggs are dumb rangers: measure distances, broadcast a RANGE_DATA line
  - PC accumulates the distance matrix, re-solves whenever updated
  - Fault tolerant: a failing node just stops contributing distances;
    remaining nodes re-solve with what they have

Modes
-----
  --simulate          Synthetic data (default). Nodes appear/fail/recover.
  --log FILE          Parse RANGE_DATA lines from an existing log file and
                      replay them (also tails the file live if --follow).
  --names egg_6 ...   Connect to real eggs over BLE and parse live output.

Expected log/BLE line format (one line per ranging round):
  [HH:MM:SS.mmm] [egg_N] RANGE_DATA src=N 7:1.234 8:2.567 6:3.890

To emit this from an egg add one log line at the end of ranger.measure():
  logger.event("RANGE_DATA", [("src", self.node_id)] + list(distances.items()))

Requirements
------------
  pip install matplotlib numpy bleak    (bleak only for --names mode)
"""

import argparse
import asyncio
import math
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

try:
    import numpy as np
except ImportError:
    print("numpy is required:  pip install numpy")
    sys.exit(1)

try:
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation
except ImportError:
    print("matplotlib is required:  pip install matplotlib")
    sys.exit(1)

# ---------------------------------------------------------------------------
# MDS solver (classic metric MDS, 2-D)
# ---------------------------------------------------------------------------

def mds_solve(node_ids, dist_matrix):
    """
    Classic metric MDS from a (possibly incomplete) pairwise distance dict.

    dist_matrix keys are (a, b) tuples with a < b.
    Returns dict of node_id -> (x, y), or None if not enough data.
    """
    n = len(node_ids)
    if n < 2:
        return None

    # Build D matrix; missing pairs filled with median of known distances.
    known = [v for v in dist_matrix.values() if v > 0]
    if not known:
        return None
    fill = float(np.median(known))

    idx = {nid: i for i, nid in enumerate(node_ids)}
    D = np.zeros((n, n))
    for i, a in enumerate(node_ids):
        for j, b in enumerate(node_ids):
            if i == j:
                continue
            key = (min(a, b), max(a, b))
            D[i, j] = dist_matrix.get(key, fill)

    # Double-centre D² → B, then eigen-decompose.
    D2 = D ** 2
    ones = np.ones((n, n)) / n
    H = np.eye(n) - ones
    B = -0.5 * H @ D2 @ H

    vals, vecs = np.linalg.eigh(B)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]

    coords = vecs[:, :2] * np.sqrt(np.maximum(vals[:2], 0))
    return {node_ids[i]: (float(coords[i, 0]), float(coords[i, 1]))
            for i in range(n)}


# ---------------------------------------------------------------------------
# Distance accumulator
# ---------------------------------------------------------------------------

class DistanceStore:
    """Thread-safe store of pairwise distances with per-pair timestamps."""

    def __init__(self, stale_s=30.0):
        self._lock   = threading.Lock()
        self._dists  = {}   # (a,b) -> distance (a < b)
        self._times  = {}   # (a,b) -> last-updated timestamp
        self._stale  = stale_s

    def update(self, src, targets):
        """
        Record distances from node src to each node in targets dict
        {node_id: distance_m}.
        """
        now = time.time()
        with self._lock:
            for tgt, d in targets.items():
                if tgt == src or d <= 0:
                    continue
                key = (min(src, tgt), max(src, tgt))
                self._dists[key] = float(d)
                self._times[key] = now

    def snapshot(self):
        """Return (node_ids_sorted, dist_matrix) pruned of stale entries."""
        now = time.time()
        with self._lock:
            fresh = {k: v for k, v in self._dists.items()
                     if now - self._times.get(k, 0) < self._stale}
        node_set = set()
        for a, b in fresh:
            node_set.add(a)
            node_set.add(b)
        return sorted(node_set), fresh


# ---------------------------------------------------------------------------
# Line parser
# ---------------------------------------------------------------------------

RANGE_RE = re.compile(
    r'\[([^\]]+)\]\s+\[([^\]]+)\]\s+RANGE_DATA\s+src=(\d+)\s+(.*)'
)
PAIR_RE = re.compile(r'(\d+):([\d.]+)')


def parse_range_line(line):
    """
    Parse a RANGE_DATA log line.
    Returns (src_id, {target_id: distance}) or None.
    """
    m = RANGE_RE.search(line)
    if not m:
        return None
    src = int(m.group(3))
    targets = {int(a): float(b) for a, b in PAIR_RE.findall(m.group(4))}
    return src, targets


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

NODE_COLOURS = ['#00bfff', '#ff69b4', '#ffd700', '#7fff00',
                '#ff4500', '#da70d6', '#20b2aa', '#ff8c00']

# True positions (metres) for the simulation — arranged loosely like a crater
SIM_NODES = {
    6: (0.0,  0.0),
    7: (2.5,  0.3),
    8: (1.2,  2.1),
    9: (3.8,  1.7),
}

NOISE_STD = 0.05   # metres of Gaussian noise on each distance measurement
RANGE_INTERVAL = 1.5   # seconds between simulated ranging rounds per node


def _true_distance(a, b):
    ax, ay = SIM_NODES[a]
    bx, by = SIM_NODES[b]
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)


class Simulator:
    """Generates synthetic RANGE_DATA events with node failure/recovery."""

    def __init__(self, store, print_fn):
        self._store   = store
        self._print   = print_fn
        self._alive   = set(SIM_NODES)
        self._t       = defaultdict(float)  # next-range time per node
        self._fail_at = {}   # node -> fail time
        self._up_at   = {}   # node -> recovery time

        # Schedule failures/recoveries for drama
        now = time.time()
        self._fail_at[9] = now + 12   # node 9 fails after 12 s
        self._up_at[9]   = now + 25   # recovers at 25 s

    def tick(self):
        now = time.time()

        # Handle failures / recoveries
        for nid, t in list(self._fail_at.items()):
            if now >= t and nid in self._alive:
                self._alive.discard(nid)
                self._print("  [SIM] Node {} FAILED".format(nid))
        for nid, t in list(self._up_at.items()):
            if now >= t and nid not in self._alive:
                self._alive.add(nid)
                self._print("  [SIM] Node {} RECOVERED".format(nid))

        # Each alive node ranges to all other alive nodes periodically
        for src in list(self._alive):
            if now < self._t[src]:
                continue
            self._t[src] = now + RANGE_INTERVAL

            targets = {}
            for tgt in self._alive:
                if tgt == src:
                    continue
                d = _true_distance(src, tgt) + np.random.normal(0, NOISE_STD)
                targets[tgt] = max(d, 0.01)

            if targets:
                self._store.update(src, targets)
                self._print("  [SIM] Node {} ranged: {}".format(
                    src, "  ".join("{}:{:.3f}m".format(t, d)
                                   for t, d in targets.items())))


# ---------------------------------------------------------------------------
# BLE monitor (--names mode)
# ---------------------------------------------------------------------------

NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

async def _ble_monitor(names, store, print_fn):
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print_fn("bleak not installed — pip install bleak")
        return

    async def watch_egg(name):
        buf = ""
        def on_notify(_h, data):
            nonlocal buf
            buf += data.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                tagged = "[{}] [{}] {}".format(ts, name, line.strip())
                parsed = parse_range_line(tagged)
                if parsed:
                    src, targets = parsed
                    store.update(src, targets)
                    print_fn("  [BLE] {}".format(tagged))

        while True:
            try:
                dev = await BleakScanner.find_device_by_name(name, timeout=8)
                if dev is None:
                    print_fn("  [BLE] {} not found, retrying…".format(name))
                    await asyncio.sleep(3)
                    continue
                async with BleakClient(dev) as c:
                    print_fn("  [BLE] {} connected".format(name))
                    await c.start_notify(NUS_TX_UUID, on_notify)
                    while c.is_connected:
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print_fn("  [BLE] {} error: {}".format(name, e))
                await asyncio.sleep(3)

    await asyncio.gather(*[watch_egg(n) for n in names])


def _ble_thread(names, store, print_fn):
    asyncio.run(_ble_monitor(names, store, print_fn))


# ---------------------------------------------------------------------------
# Log file reader (--log mode)
# ---------------------------------------------------------------------------

def _log_thread(log_path, store, print_fn, follow):
    try:
        with open(log_path, 'r') as f:
            for line in f:
                parsed = parse_range_line(line)
                if parsed:
                    src, targets = parsed
                    store.update(src, targets)
                    print_fn("  [LOG] {}".format(line.rstrip()))

            if not follow:
                return

            # Tail the file live
            print_fn("  [LOG] Replayed history — tailing for new data…")
            while True:
                line = f.readline()
                if line:
                    parsed = parse_range_line(line)
                    if parsed:
                        src, targets = parsed
                        store.update(src, targets)
                        print_fn("  [LOG] {}".format(line.rstrip()))
                else:
                    time.sleep(0.5)
    except FileNotFoundError:
        print_fn("  [LOG] File not found: {}".format(log_path))


# ---------------------------------------------------------------------------
# Live map display
# ---------------------------------------------------------------------------

def _alignment_note(positions):
    """MDS is rotation/reflection-free — note this on the plot."""
    return "Relative positions only — orientation arbitrary"


def draw_map(ax, positions, colour_map, status_lines, stale_s, dist_store):
    ax.cla()
    ax.set_aspect('equal')
    ax.grid(True, linestyle='--', alpha=0.4, color='#aaaaaa')
    ax.set_xlabel('X (m)')
    ax.set_ylabel('Y (m)')

    node_ids, dists = dist_store.snapshot()

    if len(node_ids) < 2:
        ax.set_title('Waiting for distance data from ≥2 nodes…', fontsize=11)
        ax.set_xlim(-1, 1)
        ax.set_ylim(-1, 1)
        return

    solved = mds_solve(node_ids, dists)
    if solved is None:
        ax.set_title('Solving…', fontsize=11)
        return

    names = sorted(solved)
    coords = [solved[n] for n in names]

    # Draw lines between pairs with measured distance label
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            key = (min(a, b), max(a, b))
            measured = dists.get(key)
            if measured is None:
                continue
            x0, y0 = coords[i]
            x1, y1 = coords[j]
            ax.plot([x0, x1], [y0, y1], '--', lw=0.8, color='#888888', zorder=1)
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            ax.text(mx, my, '{:.2f} m'.format(measured),
                    fontsize=7, ha='center', va='bottom', color='#555555')

    # Draw each node
    for nid, (x, y) in solved.items():
        c = colour_map.setdefault(nid,
            NODE_COLOURS[len(colour_map) % len(NODE_COLOURS)])
        ax.scatter(x, y, s=220, color=c, zorder=5,
                   edgecolors='black', linewidths=0.8)
        ax.annotate('egg_{}'.format(nid), (x, y),
                    textcoords='offset points', xytext=(10, 6),
                    fontsize=10, fontweight='bold', color=c)
        ax.annotate('({:.2f}, {:.2f})'.format(x, y), (x, y),
                    textcoords='offset points', xytext=(10, -10),
                    fontsize=7, color='#555555')

    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.5)
    pad = span * 0.35 + 0.3
    ax.set_xlim(min(xs) - pad, max(xs) + pad)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_title(
        '{} node(s) — PC-centralised MDS   |   {}'.format(
            len(solved), _alignment_note(solved)),
        fontsize=10,
    )

    # Status sidebar
    if status_lines:
        recent = status_lines[-6:]
        ax.text(0.01, 0.01, '\n'.join(recent),
                transform=ax.transAxes, fontsize=6,
                verticalalignment='bottom', color='#666666',
                fontfamily='monospace')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='PC-centralised localisation via UWB distance gossip')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--simulate', action='store_true',
                      help='Synthetic data demo (default if no other mode)')
    mode.add_argument('--log', metavar='FILE',
                      help='Parse RANGE_DATA lines from a log file')
    mode.add_argument('--names', '-n', nargs='+', metavar='NAME',
                      help='BLE egg names to connect to live')
    parser.add_argument('--follow', action='store_true',
                        help='Tail log file for new data (--log mode)')
    parser.add_argument('--stale', type=float, default=30.0,
                        help='Seconds before a distance reading is discarded (default 30)')
    parser.add_argument('--interval', type=float, default=1.0,
                        help='Plot refresh interval in seconds (default 1.0)')
    args = parser.parse_args()

    use_sim = not (args.log or args.names)

    store       = DistanceStore(stale_s=args.stale)
    colour_map  = {}
    status_lines = []
    sim         = None

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = "{} {}".format(ts, msg)
        print(line)
        status_lines.append(line)
        if len(status_lines) > 200:
            del status_lines[:100]

    # Start data source
    if use_sim:
        sim = Simulator(store, log)
        log("Mode: SIMULATION  ({} nodes, failure/recovery demo)".format(
            len(SIM_NODES)))
        log("Node 9 will fail at t+12 s and recover at t+25 s")
    elif args.log:
        log("Mode: LOG FILE  ({})".format(args.log))
        t = threading.Thread(
            target=_log_thread,
            args=(args.log, store, log, args.follow),
            daemon=True)
        t.start()
    else:
        log("Mode: BLE LIVE  ({})".format(", ".join(args.names)))
        t = threading.Thread(
            target=_ble_thread,
            args=(args.names, store, log),
            daemon=True)
        t.start()

    log("Stale timeout: {}s   |   Refresh: {}s".format(args.stale, args.interval))
    log("Close window to stop.")
    log("")

    # Build plot
    plt.ion()
    fig, ax = plt.subplots(figsize=(8, 7))
    fig.suptitle('PC-Centralised Localisation', fontsize=13, fontweight='bold')

    mode_label = ('SIMULATION' if use_sim
                  else ('LOG: ' + args.log) if args.log
                  else 'BLE: ' + ', '.join(args.names))
    fig.text(0.5, 0.01, mode_label, ha='center', fontsize=8, color='#888888')

    while plt.fignum_exists(fig.number):
        if sim:
            sim.tick()

        draw_map(ax, {}, colour_map, status_lines, args.stale, store)
        fig.canvas.draw()
        fig.canvas.flush_events()
        plt.pause(args.interval)

    log("Window closed.")


if __name__ == '__main__':
    main()

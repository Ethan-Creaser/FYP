#!/usr/bin/env python3
"""
UWB Localisation via BLE
========================
Connects to eggs running uwb_role_test.py over BLE, collects distance
readings from the tag egg, and solves positions on the PC using MDS.

The tag (UWB slot 0) outputs lines like:
    slot 1 -> 1.2340 m
    slot 2 -> 2.5670 m

This script reads those lines, builds a distance matrix, and displays
a live map.

Usage
-----
Two eggs (slot 0 = egg_6, slot 1 = egg_7):
    python3 uwb_localisation.py --slots 0:egg_6 1:egg_7

Three eggs:
    python3 uwb_localisation.py --slots 0:egg_6 1:egg_7 2:egg_8

Options:
    --stale 30       seconds before a distance reading expires (default 30)
    --interval 1.0   plot refresh rate in seconds (default 1.0)

Requirements:
    pip install bleak matplotlib numpy
"""

import argparse
import asyncio
import math
import re
import sys
import threading
import time
from datetime import datetime

try:
    import numpy as np
except ImportError:
    print("numpy required:  pip install numpy")
    sys.exit(1)

try:
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib required:  pip install matplotlib")
    sys.exit(1)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak required:  pip install bleak")
    sys.exit(1)

# ------------------------------------------------------------------ #
# BLE                                                                  #
# ------------------------------------------------------------------ #

NUS_TX_UUID   = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
RECONNECT_S   = 3

# Parses:  "  slot 1 -> 1.2340 m"
SLOT_RE = re.compile(r'slot\s+(\d+)\s*->\s*([\d.]+)\s*m')

# ------------------------------------------------------------------ #
# Distance store                                                       #
# ------------------------------------------------------------------ #

class DistanceStore:
    """Thread-safe pairwise distance store with timestamps."""

    def __init__(self, stale_s):
        self._lock   = threading.Lock()
        self._dists  = {}   # (a, b) -> metres,  a < b
        self._times  = {}
        self._stale  = stale_s

    def update(self, slot_a, slot_b, metres):
        key = (min(slot_a, slot_b), max(slot_a, slot_b))
        with self._lock:
            self._dists[key] = metres
            self._times[key] = time.time()

    def snapshot(self, slot_to_name):
        """Return (names_sorted, dist_by_name_pair) pruned of stale entries."""
        now = time.time()
        with self._lock:
            fresh = {k: v for k, v in self._dists.items()
                     if now - self._times.get(k, 0) < self._stale}

        # Convert slot keys to egg names
        named = {}
        slots_seen = set()
        for (sa, sb), d in fresh.items():
            na = slot_to_name.get(sa)
            nb = slot_to_name.get(sb)
            if na and nb:
                key = (min(na, nb), max(na, nb))
                named[key] = d
                slots_seen.add(sa)
                slots_seen.add(sb)

        names = sorted(slot_to_name[s] for s in slots_seen if s in slot_to_name)
        return names, named

# ------------------------------------------------------------------ #
# MDS solver                                                           #
# ------------------------------------------------------------------ #

def mds_solve(names, dist_by_name):
    n = len(names)
    if n < 2:
        return None

    known = list(dist_by_name.values())
    if not known:
        return None
    fill = float(np.median(known))

    D = np.zeros((n, n))
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i == j:
                continue
            key = (min(a, b), max(a, b))
            D[i, j] = dist_by_name.get(key, fill)

    D2 = D ** 2
    H  = np.eye(n) - np.ones((n, n)) / n
    B  = -0.5 * H @ D2 @ H

    vals, vecs = np.linalg.eigh(B)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    coords = vecs[:, :2] * np.sqrt(np.maximum(vals[:2], 0))

    return {names[i]: (float(coords[i, 0]), float(coords[i, 1]))
            for i in range(n)}

# ------------------------------------------------------------------ #
# BLE connection                                                       #
# ------------------------------------------------------------------ #

async def monitor_tag(egg_name, tag_slot, store, log_fn):
    """Connect to the tag egg and parse slot distance lines."""
    buf = ""

    def on_notify(_h, data):
        nonlocal buf
        buf += data.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            m = SLOT_RE.search(line)
            if m:
                anchor_slot = int(m.group(1))
                metres      = float(m.group(2))
                store.update(tag_slot, anchor_slot, metres)
                log_fn("  [{}] slot {} -> {:.4f} m".format(egg_name, anchor_slot, metres))

    while True:
        try:
            dev = await BleakScanner.find_device_by_name(egg_name, timeout=8)
            if dev is None:
                log_fn("  {} not found — retrying in {}s".format(egg_name, RECONNECT_S))
                await asyncio.sleep(RECONNECT_S)
                continue

            log_fn("  Connecting to {} ({})".format(egg_name, dev.address))
            async with BleakClient(dev) as client:
                log_fn("  {} connected".format(egg_name))
                await client.start_notify(NUS_TX_UUID, on_notify)
                while client.is_connected:
                    await asyncio.sleep(0.5)

            log_fn("  {} disconnected — reconnecting".format(egg_name))
            await asyncio.sleep(RECONNECT_S)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_fn("  {} error: {} — retrying".format(egg_name, e))
            await asyncio.sleep(RECONNECT_S)


def ble_thread_fn(tag_name, tag_slot, store, log_fn):
    asyncio.run(monitor_tag(tag_name, tag_slot, store, log_fn))

# ------------------------------------------------------------------ #
# Live map                                                             #
# ------------------------------------------------------------------ #

NODE_COLOURS = ['#00bfff', '#ff69b4', '#ffd700', '#7fff00',
                '#ff4500', '#da70d6', '#20b2aa', '#ff8c00']


def draw_map(ax, store, slot_to_name, colour_map, log_lines):
    ax.cla()
    ax.set_aspect('equal')
    ax.grid(True, linestyle='--', alpha=0.4, color='#aaaaaa')
    ax.set_xlabel('X (m)')
    ax.set_ylabel('Y (m)')

    names, dist_by_name = store.snapshot(slot_to_name)

    if len(names) < 2:
        ax.set_title('Waiting for distance data from ≥2 nodes…', fontsize=11)
        ax.set_xlim(-1, 1)
        ax.set_ylim(-1, 1)
        return

    solved = mds_solve(names, dist_by_name)
    if not solved:
        ax.set_title('Solving…')
        return

    coords = [solved[n] for n in names]

    # Lines between pairs with measured distance
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            key  = (min(a, b), max(a, b))
            d    = dist_by_name.get(key)
            if d is None:
                continue
            x0, y0 = coords[i]
            x1, y1 = coords[j]
            ax.plot([x0, x1], [y0, y1], '--', lw=0.9, color='#888888', zorder=1)
            ax.text((x0+x1)/2, (y0+y1)/2, '{:.3f} m'.format(d),
                    fontsize=7, ha='center', va='bottom', color='#555555')

    # Nodes
    for name, (x, y) in solved.items():
        c = colour_map.setdefault(name,
            NODE_COLOURS[len(colour_map) % len(NODE_COLOURS)])
        ax.scatter(x, y, s=220, color=c, zorder=5,
                   edgecolors='black', linewidths=0.8)
        ax.annotate(name, (x, y), textcoords='offset points',
                    xytext=(10, 6), fontsize=10, fontweight='bold', color=c)
        ax.annotate('({:.2f}, {:.2f})'.format(x, y), (x, y),
                    textcoords='offset points', xytext=(10, -10),
                    fontsize=7, color='#555555')

    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.5)
    pad  = span * 0.35 + 0.3
    ax.set_xlim(min(xs) - pad, max(xs) + pad)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_title('{} node(s)  —  relative positions (MDS)'.format(len(solved)),
                 fontsize=11)

    if log_lines:
        ax.text(0.01, 0.01, '\n'.join(log_lines[-5:]),
                transform=ax.transAxes, fontsize=6,
                va='bottom', color='#666666', fontfamily='monospace')

# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(
        description='Live UWB localisation from uwb_role_test.py via BLE')
    parser.add_argument(
        '--slots', nargs='+', required=True, metavar='SLOT:EGG',
        help='UWB slot to egg name mapping, e.g.  0:egg_6  1:egg_7  2:egg_8')
    parser.add_argument('--stale',    type=float, default=30.0,
                        help='Distance expiry in seconds (default 30)')
    parser.add_argument('--interval', type=float, default=1.0,
                        help='Plot refresh in seconds (default 1.0)')
    args = parser.parse_args()

    # Parse --slots 0:egg_6 1:egg_7 ...
    slot_to_name = {}
    for entry in args.slots:
        try:
            slot_str, name = entry.split(':')
            slot_to_name[int(slot_str)] = name
        except ValueError:
            print("Bad --slots entry '{}' — expected format  SLOT:egg_N".format(entry))
            sys.exit(1)

    if 0 not in slot_to_name:
        print("Slot 0 (the tag) must be included in --slots")
        sys.exit(1)

    tag_name = slot_to_name[0]
    print("Tag egg:    {} (slot 0)".format(tag_name))
    print("All slots:  {}".format(slot_to_name))
    print("Stale:      {}s   Refresh: {}s\n".format(args.stale, args.interval))

    store     = DistanceStore(stale_s=args.stale)
    colour_map = {}
    log_lines  = []

    def log(msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = "{} {}".format(ts, msg)
        print(line)
        log_lines.append(line)
        if len(log_lines) > 200:
            del log_lines[:100]

    # BLE in background thread
    t = threading.Thread(
        target=ble_thread_fn,
        args=(tag_name, 0, store, log),
        daemon=True,
    )
    t.start()

    # Live plot
    plt.ion()
    fig, ax = plt.subplots(figsize=(7, 7))
    fig.suptitle('UWB Localisation — PC side', fontsize=13, fontweight='bold')
    fig.text(0.5, 0.01,
             'Tag: {}   |   Anchors: {}'.format(
                 tag_name,
                 ', '.join(v for k, v in sorted(slot_to_name.items()) if k != 0)),
             ha='center', fontsize=8, color='#888888')

    while plt.fignum_exists(fig.number):
        draw_map(ax, store, slot_to_name, colour_map, log_lines)
        fig.canvas.draw()
        fig.canvas.flush_events()
        plt.pause(args.interval)

    print("Window closed.")


if __name__ == '__main__':
    main()

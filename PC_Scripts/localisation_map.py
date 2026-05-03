#!/usr/bin/env python3
"""
Localisation Map
Live scatter plot of egg positions, read from localisation_log.txt.

Runs alongside localisation_monitor.py — reads the same log file, touches
nothing in the monitor or the eggs.

Requirements:
    pip install matplotlib

Usage:
    python3 localisation_map.py
    python3 localisation_map.py --log path/to/localisation_log.txt
    python3 localisation_map.py --interval 1.0
"""

import argparse
import math
import re
import sys
import time

try:
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib is required:  pip install matplotlib")
    sys.exit(1)

POSITION_RE = re.compile(
    r'\[([^\]]+)\]\s+\[([^\]]+)\]\s+Position:\s+([-\d.]+),\s+([-\d.]+)'
)
SESSION_RE = re.compile(r'^=== SESSION')

NODE_COLOURS = ['#00bfff', '#ff69b4', '#ffd700', '#7fff00', '#ff4500',
                '#da70d6', '#20b2aa', '#ff8c00']


def read_current_session(log_path):
    """
    Parse positions from the most recent session in the log.
    Returns dict of egg_name -> (x, y) using the last known position per egg.
    """
    try:
        with open(log_path, 'r') as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return {}

    session_start = 0
    for i, line in enumerate(lines):
        if SESSION_RE.match(line):
            session_start = i

    positions = {}
    for line in lines[session_start:]:
        m = POSITION_RE.search(line)
        if m:
            egg_name = m.group(2)
            x, y = float(m.group(3)), float(m.group(4))
            positions[egg_name] = (x, y)

    return positions


def draw_map(ax, positions, colour_map):
    ax.cla()
    ax.set_aspect('equal')
    ax.grid(True, linestyle='--', alpha=0.4, color='#aaaaaa')
    ax.set_xlabel('X (m)', fontsize=10)
    ax.set_ylabel('Y (m)', fontsize=10)

    if not positions:
        ax.set_title('Waiting for position data…', fontsize=11)
        ax.set_xlim(-1, 1)
        ax.set_ylim(-1, 1)
        return

    names = sorted(positions)
    coords = [positions[n] for n in names]

    # Draw lines between every pair with distance label
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            x0, y0 = coords[i]
            x1, y1 = coords[j]
            d = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
            ax.plot([x0, x1], [y0, y1], '--', lw=1.0, color='#888888', zorder=1)
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            ax.text(mx, my, '{:.3f} m'.format(d),
                    fontsize=8, ha='center', va='bottom', color='#555555')

    # Draw each egg
    for name, (x, y) in positions.items():
        c = colour_map[name]
        ax.scatter(x, y, s=220, color=c, zorder=5,
                   edgecolors='black', linewidths=0.8)
        ax.annotate(
            name,
            (x, y),
            textcoords='offset points',
            xytext=(10, 6),
            fontsize=10,
            fontweight='bold',
            color=c,
        )
        ax.annotate(
            '({:.3f}, {:.3f})'.format(x, y),
            (x, y),
            textcoords='offset points',
            xytext=(10, -10),
            fontsize=7,
            color='#555555',
        )

    # Axis limits with padding
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.5)
    pad = span * 0.35 + 0.3
    ax.set_xlim(min(xs) - pad, max(xs) + pad)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_title('{} node(s) — current session'.format(len(positions)), fontsize=11)


def main():
    parser = argparse.ArgumentParser(description='Live egg localisation map')
    parser.add_argument(
        '--log', '-l', default='localisation_log.txt',
        help='Log file to read (default: localisation_log.txt)',
    )
    parser.add_argument(
        '--interval', '-i', type=float, default=1.0,
        help='Refresh interval in seconds (default: 1.0)',
    )
    args = parser.parse_args()

    colour_map = {}
    colour_idx = 0

    plt.ion()
    fig, ax = plt.subplots(figsize=(7, 7))
    fig.suptitle('Egg Localisation Map', fontsize=13, fontweight='bold')
    fig.text(0.5, 0.01, 'Reading: {}'.format(args.log),
             ha='center', fontsize=8, color='#888888')

    print("Watching: {}".format(args.log))
    print("Interval: {}s  —  close the window to stop.\n".format(args.interval))

    while plt.fignum_exists(fig.number):
        positions = read_current_session(args.log)

        for name in positions:
            if name not in colour_map:
                colour_map[name] = NODE_COLOURS[colour_idx % len(NODE_COLOURS)]
                colour_idx += 1

        draw_map(ax, positions, colour_map)
        fig.canvas.draw()
        fig.canvas.flush_events()
        plt.pause(args.interval)

    print("Window closed — exiting.")


if __name__ == '__main__':
    main()

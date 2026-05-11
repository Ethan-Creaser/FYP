"""Build a 2D position map of all eggs from uwb_scan.csv.

Reads the CSV produced by auto_uwb_scan.py or send_uwb_config.py, constructs
a pairwise distance dict, then solves positions using the project's trilateration
solver (Trilat/code git/localise.py  →  solve_from_distance_matrix).

Usage:
    python map_from_csv.py
    python map_from_csv.py --csv uwb_scan.csv --out map.png

Requires:
    pip install matplotlib
"""

import argparse
import csv
import os
import sys
from collections import defaultdict

# ── Import project trilateration solver ──────────────────────────────────────
_TRILAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "Trilat", "code git")
sys.path.insert(0, _TRILAT_PATH)

try:
    from localise import solve_from_distance_matrix
except ImportError as _e:
    print("ERROR: cannot import localise.py from Trilat/code git:", _e)
    sys.exit(1)

try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
except ImportError:
    print("matplotlib required:  pip install matplotlib")
    sys.exit(1)

_CSV_PATH = "uwb_scan.csv"

NODE_COLOURS = [
    '#00bfff', '#ff69b4', '#ffd700', '#7fff00',
    '#ff4500', '#da70d6', '#20b2aa', '#ff8c00',
    '#9370db', '#32cd32', '#dc143c', '#4169e1',
]


# ── CSV loading ───────────────────────────────────────────────────────────────

def load_csv(path):
    """
    Returns:
        measurements : list of (node_id_a, node_id_b, distance_m)

    Each row: node_id = tag node, slot = anchor's uwb_id = anchor's node_id.
    All scanned rows have uwb_id=0 (temporary tag assignment), so slot is used
    directly as the anchor node_id.
    """
    raw_dist = defaultdict(list)   # (node_id_tag, slot) -> [distances]

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                node_id = int(row["node_id"])
                slot    = int(row["slot"])
                dist    = float(row["distance_m"])
            except (KeyError, ValueError):
                continue
            raw_dist[(node_id, slot)].append(dist)

    # Median of repeated measurements; slot == anchor node_id
    measurements = []
    for (node_a, slot_b), dists in raw_dist.items():
        node_b = slot_b          # slot number IS the anchor's node_id
        median_dist = sum(dists) / len(dists)   # mean (no numpy needed)
        measurements.append((node_a, node_b, median_dist))

    return measurements


# ── Plot ──────────────────────────────────────────────────────────────────────

def plot_map(coords_dict, measurements, out_path=None):
    """
    coords_dict  : {node_id: (x, y, z)}  from solve_from_distance_matrix
    measurements : [(node_a, node_b, dist_m), ...]  for drawing distance lines
    """
    node_ids = sorted(coords_dict.keys())
    n = len(node_ids)
    colour_map = {nid: NODE_COLOURS[i % len(NODE_COLOURS)]
                  for i, nid in enumerate(node_ids)}

    fig, ax = plt.subplots(figsize=(9, 8))
    ax.set_aspect("equal")
    ax.grid(True, linestyle="--", alpha=0.4, color="#aaaaaa")
    ax.set_xlabel("X (m)", fontsize=11)
    ax.set_ylabel("Y (m)", fontsize=11)
    fig.suptitle("Egg Positions — Trilateration (MDS) from UWB distances",
                 fontsize=13, fontweight="bold")

    xs = {nid: coords_dict[nid][0] for nid in node_ids}
    ys = {nid: coords_dict[nid][1] for nid in node_ids}

    # Build a lookup of measured distances for line labels
    dist_lookup = {}
    for a, b, d in measurements:
        dist_lookup[(a, b)] = d
        dist_lookup[(b, a)] = d

    # Draw lines between pairs that have a measured distance
    drawn = set()
    for a, b, d in measurements:
        pair = (min(a, b), max(a, b))
        if pair in drawn or a not in coords_dict or b not in coords_dict:
            continue
        drawn.add(pair)
        ax.plot([xs[a], xs[b]], [ys[a], ys[b]],
                "--", lw=0.9, color="#999999", zorder=1)
        ax.text((xs[a] + xs[b]) / 2,
                (ys[a] + ys[b]) / 2,
                "{:.2f} m".format(d),
                fontsize=7, ha="center", va="bottom", color="#555555")

    # Draw nodes
    for nid in node_ids:
        c = colour_map[nid]
        x, y = xs[nid], ys[nid]
        ax.scatter(x, y, s=250, color=c, zorder=5,
                   edgecolors="black", linewidths=0.9)
        ax.annotate("egg_{}".format(nid), (x, y),
                    textcoords="offset points", xytext=(12, 6),
                    fontsize=10, fontweight="bold", color=c)
        ax.annotate("({:.2f}, {:.2f})".format(x, y), (x, y),
                    textcoords="offset points", xytext=(12, -10),
                    fontsize=7, color="#555555")

    # Legend
    patches = [mpatches.Patch(color=colour_map[nid], label="egg_{}".format(nid))
               for nid in node_ids]
    ax.legend(handles=patches, loc="upper right", fontsize=8)

    # Axis limits with padding
    all_x = list(xs.values())
    all_y = list(ys.values())
    span = max(max(all_x) - min(all_x), max(all_y) - min(all_y), 0.5)
    pad  = span * 0.35 + 0.4
    ax.set_xlim(min(all_x) - pad, max(all_x) + pad)
    ax.set_ylim(min(all_y) - pad, max(all_y) + pad)

    # Coordinate table
    rows_str = ["{:<10} {:>8} {:>8}".format(
        "egg_{}".format(nid), "{:.2f}".format(xs[nid]), "{:.2f}".format(ys[nid]))
        for nid in node_ids]
    header = "{:<10} {:>8} {:>8}".format("node", "x (m)", "y (m)")
    table  = "\n".join([header, "-" * 28] + rows_str)
    ax.text(0.01, 0.01, table, transform=ax.transAxes,
            fontsize=7, va="bottom", fontfamily="monospace",
            color="#444444",
            bbox=dict(boxstyle="round,pad=0.4", fc="white", alpha=0.8))

    fig.tight_layout()

    if out_path:
        fig.savefig(out_path, dpi=150)
        print("Saved to {}".format(out_path))
    else:
        plt.show()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Map egg positions from uwb_scan.csv using project trilateration solver.")
    parser.add_argument("--csv", default=_CSV_PATH,
                        help="Path to CSV file (default: uwb_scan.csv)")
    parser.add_argument("--out", default=None,
                        help="Save plot to file instead of displaying (e.g. map.png)")
    args = parser.parse_args()

    print("Reading {}...".format(args.csv))
    try:
        measurements = load_csv(args.csv)
    except FileNotFoundError:
        print("ERROR: {} not found.".format(args.csv))
        sys.exit(1)

    if not measurements:
        print("No measurements found in CSV.")
        sys.exit(1)

    print("{} pairwise measurements loaded.".format(len(measurements)))
    for a, b, d in sorted(measurements, key=lambda x: (x[0], x[1])):
        print("  egg_{} <-> egg_{} : {:.4f} m".format(a, b, d))

    # Build inputs for the trilateration solver
    node_ids = sorted(set(a for a, b, d in measurements) |
                      set(b for a, b, d in measurements))
    dist_matrix = {(a, b): d for a, b, d in measurements}

    if len(node_ids) < 2:
        print("Need at least 2 nodes to solve.")
        sys.exit(1)

    print("\nSolving positions via trilateration (MDS)...")
    coords_dict = solve_from_distance_matrix(node_ids, dist_matrix)

    if not coords_dict:
        print("Solver returned no positions.")
        sys.exit(1)

    print("\nSolved positions (node_0 at origin, node_0→node_1 = +X):")
    for nid in sorted(coords_dict):
        x, y, z = coords_dict[nid]
        print("  egg_{}: x={:.3f}m  y={:.3f}m".format(nid, x, y))

    plot_map(coords_dict, measurements, out_path=args.out)


if __name__ == "__main__":
    main()

"""Build a 2D position map of all eggs from uwb_scan.csv.

Reads the CSV produced by auto_uwb_scan.py or send_uwb_config.py, constructs
a pairwise distance dict, then solves positions using the project's trilateration
solver (Trilat/code git/localise.py  →  solve_from_distance_matrix).

Usage:
    python map_from_csv.py --csv localisation_tests/uwb_scan_20260513_143022.csv
    python map_from_csv.py --csv localisation_tests/uwb_scan_20260513_143022.csv --out map.png
    python map_from_csv.py --csv ... --uwb-map-file uwb_id_map.txt

uwb_id_map.txt format (one entry per line, # = comment):
    # egg_id:uwb_id
    8:5
    6:3

Requires:
    pip install matplotlib
"""

import argparse
import csv
import math
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

_CSV_DIR  = "localisation_tests"


def _latest_csv():
    import glob
    files = sorted(glob.glob(os.path.join(_CSV_DIR, "uwb_scan_*.csv")))
    return files[-1] if files else None

NODE_COLOURS = [
    '#00bfff', '#ff69b4', '#ffd700', '#7fff00',
    '#ff4500', '#da70d6', '#20b2aa', '#ff8c00',
    '#9370db', '#32cd32', '#dc143c', '#4169e1',
]


# ── CSV loading ───────────────────────────────────────────────────────────────

def load_csv(path, uwb_map=None):
    """
    Returns:
        measurements : list of (node_id_a, node_id_b, distance_m)

    Each row: node_id = tag node, slot = anchor's uwb_id.
    By default slot == anchor node_id, but uwb_map overrides this for eggs
    whose UWB ID differs from their node/egg ID (e.g. egg 8 with UWB ID 5).

    uwb_map : {uwb_id: node_id}  e.g. {5: 8}
    """
    uwb_map = uwb_map or {}
    raw_dist = defaultdict(list)   # (node_id_tag, anchor_node_id) -> [distances]

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                node_id = int(row["node_id"])
                slot    = int(row["slot"])
                dist    = float(row["distance_m"])
            except (KeyError, ValueError):
                continue
            anchor_node_id = uwb_map.get(slot, slot)
            raw_dist[(node_id, anchor_node_id)].append(dist)

    measurements = []
    for (node_a, node_b), dists in raw_dist.items():
        median_dist = sum(dists) / len(dists)
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


# ── Orientation ──────────────────────────────────────────────────────────────

def orient_coords(coords_dict, bottom_egg, top_egg, left_egg=None):
    """Rotate (and optionally reflect) coordinates so bottom_egg is at the bottom
    and top_egg is at the top. If left_egg is given, it resolves the mirror-image
    ambiguity: it must end up to the left of the bottom→top axis."""
    missing = [e for e in [bottom_egg, top_egg] if e not in coords_dict]
    if missing:
        print("WARNING: orientation egg(s) {} not in solution — skipping orientation.".format(missing))
        return coords_dict

    x_b, y_b = coords_dict[bottom_egg][0], coords_dict[bottom_egg][1]
    x_t, y_t = coords_dict[top_egg][0],    coords_dict[top_egg][1]

    current_angle = math.atan2(y_t - y_b, x_t - x_b)
    target_angle  = math.pi / 2   # 90° = straight up
    phi = target_angle - current_angle
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)

    rotated = {
        nid: (x * cos_phi - y * sin_phi,
              x * sin_phi + y * cos_phi,
              z)
        for nid, (x, y, z) in coords_dict.items()
    }

    if left_egg is not None:
        if left_egg not in rotated:
            print("WARNING: --left egg_{} not in solution — skipping reflection check.".format(left_egg))
            return rotated

        rx_b, ry_b = rotated[bottom_egg][0], rotated[bottom_egg][1]
        rx_t, ry_t = rotated[top_egg][0],    rotated[top_egg][1]
        rx_l, ry_l = rotated[left_egg][0],   rotated[left_egg][1]

        # 2D cross product: positive = left_egg is to the left of bottom→top
        cross = (rx_t - rx_b) * (ry_l - ry_b) - (ry_t - ry_b) * (rx_l - rx_b)

        if cross < 0:
            # Mirror image — reflect about the vertical axis (negate x)
            rotated = {nid: (-x, y, z) for nid, (x, y, z) in rotated.items()}
            print("  reflection applied (egg_{} was on wrong side).".format(left_egg))

    return rotated


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Map egg positions from uwb_scan.csv using project trilateration solver.")
    parser.add_argument("--csv", default=None,
                        help="Path to CSV file (default: most recent file in localisation_tests/)")
    parser.add_argument("--out", default=None,
                        help="Save plot to file instead of displaying (e.g. map.png)")
    parser.add_argument("--bottom", type=int, default=None, metavar="EGG_ID",
                        help="Egg ID to place at the bottom")
    parser.add_argument("--top", type=int, default=None, metavar="EGG_ID",
                        help="Egg ID to place at the top")
    parser.add_argument("--left", type=int, default=None, metavar="EGG_ID",
                        help="Egg ID that should appear to the left of the bottom→top axis (fixes mirror-image flip)")
    parser.add_argument("--uwb-map-file", default="uwb_id_map.txt", metavar="FILE",
                        help="Path to txt file mapping egg IDs to UWB slot IDs. "
                             "One 'egg_id:uwb_id' entry per line. Lines starting with # are ignored. "
                             "(default: uwb_id_map.txt)")
    args = parser.parse_args()

    uwb_map = {}
    if args.uwb_map_file and os.path.exists(args.uwb_map_file):
        try:
            with open(args.uwb_map_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        node_id, uwb_id = line.split(":")
                        uwb_map[int(uwb_id)] = int(node_id)
                    except (ValueError, AttributeError):
                        print("WARNING: ignoring invalid line '{}' in {}".format(line, args.uwb_map_file))
        except FileNotFoundError:
            if args.uwb_map_file != "uwb_id_map.txt":
                print("ERROR: uwb-map-file '{}' not found.".format(args.uwb_map_file))
                sys.exit(1)
    if uwb_map:
        print("UWB ID remapping (from {}):".format(args.uwb_map_file))
        for u, n in sorted(uwb_map.items()):
            print("  slot {} → egg {}".format(u, n))

    csv_path = args.csv or _latest_csv()
    if csv_path is None:
        print("ERROR: no CSV file found in '{}'.".format(_CSV_DIR))
        sys.exit(1)
    if args.csv is None:
        print("Using most recent scan: {}".format(csv_path))

    print("Reading {}...".format(csv_path))
    try:
        measurements = load_csv(csv_path, uwb_map=uwb_map)
    except FileNotFoundError:
        print("ERROR: {} not found.".format(csv_path))
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

    if args.bottom is not None and args.top is not None:
        print("Orienting: egg_{} → bottom, egg_{} → top{}...".format(
            args.bottom, args.top,
            ", egg_{} → left".format(args.left) if args.left is not None else ""))
        coords_dict = orient_coords(coords_dict, args.bottom, args.top, left_egg=args.left)
    elif (args.bottom is None) != (args.top is None):
        print("WARNING: provide both --bottom and --top to orient the map.")

    print("\nSolved positions:")
    for nid in sorted(coords_dict):
        x, y, z = coords_dict[nid]
        print("  egg_{}: x={:.3f}m  y={:.3f}m".format(nid, x, y))

    plot_map(coords_dict, measurements, out_path=args.out)



if __name__ == "__main__":
    main()

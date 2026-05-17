"""Score UWB trilateration map accuracy against a ground-truth map.

Runs the same solver pipeline as map_from_csv.py, then rigidly aligns the
solved positions to the truth positions (rotation + translation, no scaling,
no reflection) using Procrustes / SVD, and reports per-egg errors.

Truth CSV format  (egg_id,x,y):
    egg_id,x,y
    6,0.00,0.00
    7,1.50,0.00
    8,1.50,2.00
    9,0.00,2.00

Usage:
    python score_map.py --truth truth_map.csv
    python score_map.py --truth truth_map.csv --csv localisation_tests/uwb_scan_xxx.csv
    python score_map.py --truth truth_map.csv --bottom 6 --top 9 --left 7
    python score_map.py --truth truth_map.csv --out score.png

Requires:
    pip install matplotlib numpy
"""

import argparse
import csv
import math
import os
import sys
from collections import defaultdict

import numpy as np

# ── Import shared solver + helpers from map_from_csv ─────────────────────────
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
_TRILAT_PATH = os.path.join(_SCRIPTS, "..", "Trilat", "code git")
sys.path.insert(0, _TRILAT_PATH)
sys.path.insert(0, _SCRIPTS)

try:
    from localise import solve_from_distance_matrix
except ImportError as e:
    print("ERROR: cannot import localise.py from Trilat/code git:", e)
    sys.exit(1)

try:
    from map_from_csv import load_csv, orient_coords, _CSV_DIR, _latest_csv
except ImportError as e:
    print("ERROR: cannot import map_from_csv.py:", e)
    sys.exit(1)

try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False


# ── Truth CSV loading ─────────────────────────────────────────────────────────

def load_truth(path):
    """Load ground-truth positions from a CSV with columns: egg_id, x, y"""
    truth = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                egg_id = int(row["egg_id"])
                x      = float(row["x"])
                y      = float(row["y"])
            except (KeyError, ValueError):
                continue
            truth[egg_id] = (x, y)
    return truth


# ── Procrustes alignment (rotation + translation, no scale, no reflection) ───

def rigid_align(solved_pts, truth_pts):
    """
    Find the rigid-body transform (R, t) that best maps solved_pts onto truth_pts.

    solved_pts, truth_pts : np.ndarray of shape (N, 2), rows matched by egg ID.

    Returns:
        aligned  : (N, 2) array — solved_pts after applying R and t
        R        : (2, 2) rotation matrix
        t        : (2,)   translation vector
    """
    centroid_s = solved_pts.mean(axis=0)
    centroid_t = truth_pts.mean(axis=0)

    A = solved_pts - centroid_s   # centred solved
    B = truth_pts  - centroid_t   # centred truth

    H = A.T @ B
    U, _, Vt = np.linalg.svd(H)

    # Ensure proper rotation (det = +1, not a reflection)
    d = np.linalg.det(Vt.T @ U.T)
    D = np.diag([1.0, d])
    R = Vt.T @ D @ U.T

    t = centroid_t - R @ centroid_s
    aligned = (R @ solved_pts.T).T + t
    return aligned, R, t


# ── Scoring ───────────────────────────────────────────────────────────────────

def score(solved_dict, truth_dict):
    """
    Align solved positions to truth and return per-egg errors (metres).

    Returns:
        errors      : {egg_id: float}  Euclidean error after alignment
        aligned_dict: {egg_id: (x, y)} solved positions after alignment
        common_ids  : list of egg IDs present in both maps
    """
    common_ids = sorted(set(solved_dict) & set(truth_dict))
    if len(common_ids) < 2:
        print("ERROR: need at least 2 eggs in common between solved and truth maps.")
        sys.exit(1)

    solved_arr = np.array([[solved_dict[i][0], solved_dict[i][1]] for i in common_ids])
    truth_arr  = np.array([[truth_dict[i][0],  truth_dict[i][1]]  for i in common_ids])

    aligned_arr, R, t = rigid_align(solved_arr, truth_arr)

    errors = {}
    for idx, eid in enumerate(common_ids):
        dx = aligned_arr[idx, 0] - truth_arr[idx, 0]
        dy = aligned_arr[idx, 1] - truth_arr[idx, 1]
        errors[eid] = math.sqrt(dx*dx + dy*dy)

    aligned_dict = {eid: (aligned_arr[i, 0], aligned_arr[i, 1])
                    for i, eid in enumerate(common_ids)}

    angle_deg = math.degrees(math.atan2(R[1, 0], R[0, 0]))
    print("  Alignment: rotation {:.1f}°, translation ({:.3f}, {:.3f}) m".format(
        angle_deg, t[0], t[1]))

    return errors, aligned_dict, common_ids


# ── Plot ──────────────────────────────────────────────────────────────────────

NODE_COLOURS = [
    '#00bfff', '#ff69b4', '#ffd700', '#7fff00',
    '#ff4500', '#da70d6', '#20b2aa', '#ff8c00',
    '#9370db', '#32cd32', '#dc143c', '#4169e1',
]


def plot_score(aligned_dict, truth_dict, errors, common_ids, out_path=None):
    if not _HAS_MPL:
        print("matplotlib not installed — skipping plot.")
        return

    colour_map = {eid: NODE_COLOURS[i % len(NODE_COLOURS)]
                  for i, eid in enumerate(common_ids)}

    fig, ax = plt.subplots(figsize=(9, 8))
    ax.set_aspect("equal")
    ax.grid(True, linestyle="--", alpha=0.4, color="#aaaaaa")
    ax.set_xlabel("X (m)", fontsize=11)
    ax.set_ylabel("Y (m)", fontsize=11)
    fig.suptitle("Map Accuracy — Solved vs Ground Truth", fontsize=13, fontweight="bold")

    for eid in common_ids:
        c = colour_map[eid]
        tx, ty = truth_dict[eid]
        sx, sy = aligned_dict[eid]

        # Error line
        ax.plot([tx, sx], [ty, sy], "-", color=c, lw=1.5, alpha=0.7, zorder=2)

        # Truth marker (filled circle)
        ax.scatter(tx, ty, s=220, color=c, zorder=5,
                   edgecolors="black", linewidths=1.2)
        ax.annotate("egg_{}\n(truth)".format(eid), (tx, ty),
                    textcoords="offset points", xytext=(10, 6),
                    fontsize=8, color="black")

        # Solved marker (open circle)
        ax.scatter(sx, sy, s=180, facecolors="none", edgecolors=c,
                   linewidths=2, zorder=5)
        ax.annotate("{:.2f} m".format(errors[eid]), ((tx+sx)/2, (ty+sy)/2),
                    textcoords="offset points", xytext=(4, 4),
                    fontsize=7, color=c)

    # Legend
    legend_handles = [
        mpatches.Patch(color=NODE_COLOURS[i % len(NODE_COLOURS)],
                       label="egg_{}  err={:.3f} m".format(eid, errors[eid]))
        for i, eid in enumerate(common_ids)
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=8)

    # Stats box
    errs = list(errors.values())
    mean_e   = sum(errs) / len(errs)
    rmse     = math.sqrt(sum(e*e for e in errs) / len(errs))
    max_e    = max(errs)
    median_e = sorted(errs)[len(errs) // 2]

    stats = (
        "n = {}  eggs\n"
        "Mean  error : {:.3f} m\n"
        "Median error: {:.3f} m\n"
        "RMSE        : {:.3f} m\n"
        "Max  error  : {:.3f} m"
    ).format(len(errs), mean_e, median_e, rmse, max_e)

    ax.text(0.01, 0.01, stats, transform=ax.transAxes,
            fontsize=8, va="bottom", fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.4", fc="white", alpha=0.85))

    # Axis limits
    all_x = [v[0] for v in truth_dict.values()] + [v[0] for v in aligned_dict.values()]
    all_y = [v[1] for v in truth_dict.values()] + [v[1] for v in aligned_dict.values()]
    span = max(max(all_x) - min(all_x), max(all_y) - min(all_y), 0.5)
    pad  = span * 0.25 + 0.4
    ax.set_xlim(min(all_x) - pad, max(all_x) + pad)
    ax.set_ylim(min(all_y) - pad, max(all_y) + pad)

    fig.tight_layout()
    if out_path:
        fig.savefig(out_path, dpi=150)
        print("Saved plot to {}".format(out_path))
    else:
        plt.show()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Score UWB map accuracy against a ground-truth CSV.")
    parser.add_argument("--truth", required=True, metavar="FILE",
                        help="Ground-truth CSV (columns: egg_id, x, y)")
    parser.add_argument("--csv", default=None,
                        help="UWB scan CSV (default: most recent in localisation_tests/)")
    parser.add_argument("--uwb-map-file", default="uwb_id_map.txt", metavar="FILE",
                        help="UWB slot→egg ID map (default: uwb_id_map.txt)")
    parser.add_argument("--bottom", type=int, default=None, metavar="EGG_ID",
                        help="Egg to place at the bottom before scoring")
    parser.add_argument("--top", type=int, default=None, metavar="EGG_ID",
                        help="Egg to place at the top before scoring")
    parser.add_argument("--left", type=int, default=None, metavar="EGG_ID",
                        help="Egg that should be to the left (fixes mirror-image flip)")
    parser.add_argument("--out", default=None,
                        help="Save plot to file instead of displaying (e.g. score.png)")
    args = parser.parse_args()

    # ── UWB ID map ────────────────────────────────────────────────────────────
    uwb_map = {}
    if args.uwb_map_file and os.path.exists(args.uwb_map_file):
        with open(args.uwb_map_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    node_id, uwb_id = line.split(":")
                    uwb_map[int(uwb_id)] = int(node_id)
                except (ValueError, AttributeError):
                    print("WARNING: ignoring invalid line '{}'".format(line))
        if uwb_map:
            print("UWB remapping: {}".format(
                ", ".join("slot{}→egg{}".format(u, n) for u, n in sorted(uwb_map.items()))))

    # ── Solved map ────────────────────────────────────────────────────────────
    csv_path = args.csv or _latest_csv()
    if csv_path is None:
        print("ERROR: no CSV found in '{}'.".format(_CSV_DIR))
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

    node_ids    = sorted(set(a for a, b, d in measurements) |
                         set(b for a, b, d in measurements))
    dist_matrix = {(a, b): d for a, b, d in measurements}

    print("Solving positions...")
    coords_dict = solve_from_distance_matrix(node_ids, dist_matrix)
    if not coords_dict:
        print("Solver returned no positions.")
        sys.exit(1)

    # Optional orientation before scoring
    if args.bottom is not None and args.top is not None:
        print("Orienting: egg_{} → bottom, egg_{} → top{}...".format(
            args.bottom, args.top,
            ", egg_{} → left".format(args.left) if args.left else ""))
        coords_dict = orient_coords(coords_dict, args.bottom, args.top,
                                    left_egg=args.left)

    solved_dict = {nid: (x, y) for nid, (x, y, z) in coords_dict.items()}

    # ── Truth map ─────────────────────────────────────────────────────────────
    print("Reading truth map: {}".format(args.truth))
    try:
        truth_dict = load_truth(args.truth)
    except FileNotFoundError:
        print("ERROR: truth file '{}' not found.".format(args.truth))
        sys.exit(1)

    if not truth_dict:
        print("No positions found in truth file.")
        sys.exit(1)

    # ── Score ─────────────────────────────────────────────────────────────────
    print("\nAligning solved map to truth (rotation + translation)...")
    errors, aligned_dict, common_ids = score(solved_dict, truth_dict)

    print("\nPer-egg errors:")
    for eid in common_ids:
        print("  egg_{}: {:.4f} m".format(eid, errors[eid]))

    errs     = list(errors.values())
    mean_e   = sum(errs) / len(errs)
    rmse     = math.sqrt(sum(e*e for e in errs) / len(errs))
    max_e    = max(errs)
    median_e = sorted(errs)[len(errs) // 2]

    print("\nSummary ({} eggs):".format(len(errs)))
    print("  Mean   error : {:.4f} m".format(mean_e))
    print("  Median error : {:.4f} m".format(median_e))
    print("  RMSE         : {:.4f} m".format(rmse))
    print("  Max    error : {:.4f} m".format(max_e))

    solved_only = set(solved_dict) - set(truth_dict)
    truth_only  = set(truth_dict)  - set(solved_dict)
    if solved_only:
        print("\nWARNING: eggs in solved but not in truth: {}".format(sorted(solved_only)))
    if truth_only:
        print("WARNING: eggs in truth but not in solved: {}".format(sorted(truth_only)))

    plot_score(aligned_dict, truth_dict, errors, common_ids, out_path=args.out)


if __name__ == "__main__":
    main()

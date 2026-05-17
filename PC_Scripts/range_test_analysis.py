"""Range Test Analysis — Test 1 (RSSI & RTT vs Distance)

Reads one or more range_test_results/range_*.csv files produced by
range_test_logger.py and generates the validation charts required for
Section 5.2 Test 1:

  1. Line chart  — RSSI mean ± std vs distance
  2. Line chart  — RTT  mean ± std vs distance
  3. Boxplot     — RSSI distribution at each distance step
  4. Boxplot     — RTT  distribution at each distance step (if data available)

Requires: pip install matplotlib pandas numpy

Usage:
    python range_test_analysis.py                           # auto-find latest CSV
    python range_test_analysis.py range_test_results/range_20260517_120000.csv
    python range_test_analysis.py range_test_results/range_A.csv range_test_results/range_B.csv
    python range_test_analysis.py --save                    # save PNGs instead of showing
    python range_test_analysis.py --kind DATA               # only use DATA (forward-path) samples
    python range_test_analysis.py --kind ACK                # only use ACK (reverse-path) samples
"""

import argparse
import glob
import os
import sys

try:
    import pandas as pd
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
except ImportError:
    print("Required: pip install matplotlib pandas numpy")
    sys.exit(1)


# ── Data loading ─────────────────────────────────────────────────────────────

def _latest_csv():
    candidates = sorted(glob.glob(os.path.join("range_test_results", "range_*.csv")))
    return candidates[-1] if candidates else None


def load_csvs(paths, kind_filter=None):
    """Load and concatenate one or more range CSV files.

    Args:
        paths:       list of file paths
        kind_filter: 'DATA', 'ACK', or None (use all)

    Returns:
        pandas DataFrame with columns:
            distance_m, rssi_dbm, snr_db, rtt_ms, kind, pc_timestamp_ms
    """
    frames = []
    for p in paths:
        try:
            df = pd.read_csv(p)
            df["source_file"] = os.path.basename(p)
            frames.append(df)
            print("Loaded {} rows from {}".format(len(df), p))
        except Exception as e:
            print("WARNING: could not load {}: {}".format(p, e))

    if not frames:
        print("ERROR: no data loaded.")
        sys.exit(1)

    data = pd.concat(frames, ignore_index=True)

    # Coerce numeric columns
    for col in ["distance_m", "rssi_dbm", "snr_db", "rtt_ms"]:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    if kind_filter:
        data = data[data["kind"] == kind_filter]
        print("Filtered to kind={} → {} rows".format(kind_filter, len(data)))

    data = data.sort_values("distance_m")
    return data


# ── Statistics ───────────────────────────────────────────────────────────────

def compute_stats(data, metric):
    """Return grouped mean, std, and list of values per distance for a metric."""
    valid   = data.dropna(subset=[metric])
    grouped = valid.groupby("distance_m")[metric]
    means   = grouped.mean()
    stds    = grouped.std().fillna(0)
    values  = {d: grp.values for d, grp in grouped}
    return means, stds, values


# ── Plots ────────────────────────────────────────────────────────────────────

def plot_line(ax, distances, means, stds, ylabel, title, color):
    ax.plot(distances, means.values, marker="o", color=color, linewidth=2, label="Mean")
    ax.fill_between(
        distances,
        means.values - stds.reindex(distances).fillna(0).values,
        means.values + stds.reindex(distances).fillna(0).values,
        alpha=0.25, color=color, label="±1 std"
    )
    ax.set_xlabel("Distance (m)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.set_xlim(left=min(distances) - 0.1)


def plot_box(ax, distances, values, ylabel, title, color):
    box_data = [values[d] for d in distances if d in values and len(values[d]) > 0]
    labels   = ["{:.1f}".format(d) for d in distances if d in values and len(values[d]) > 0]

    bp = ax.boxplot(box_data, patch_artist=True, medianprops=dict(color="black", linewidth=2))
    for patch in bp["boxes"]:
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel("Distance (m)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)


def make_figure(data, save, out_prefix="range_analysis"):
    distances = sorted(data["distance_m"].dropna().unique())

    has_rssi = data["rssi_dbm"].notna().any()
    has_rtt  = data["rtt_ms"].notna().any()

    n_cols = 2
    n_rows = (1 if has_rssi else 0) + (1 if has_rtt else 0)

    if n_rows == 0:
        print("No RSSI or RTT data found.")
        return

    fig = plt.figure(figsize=(14, 5 * n_rows))
    gs  = gridspec.GridSpec(n_rows, n_cols, figure=fig, hspace=0.45, wspace=0.35)
    fig.suptitle("Range Test — Signal Quality vs Distance", fontsize=14, fontweight="bold")

    row = 0

    # ── RSSI plots ────────────────────────────────────────────────────────────
    if has_rssi:
        rssi_means, rssi_stds, rssi_vals = compute_stats(data, "rssi_dbm")

        ax_line = fig.add_subplot(gs[row, 0])
        plot_line(ax_line, distances, rssi_means, rssi_stds,
                  "RSSI (dBm)", "RSSI Mean vs Distance", "#1f77b4")

        ax_box = fig.add_subplot(gs[row, 1])
        plot_box(ax_box, distances, rssi_vals,
                 "RSSI (dBm)", "RSSI Distribution per Step", "#1f77b4")

        row += 1

    # ── RTT plots ─────────────────────────────────────────────────────────────
    if has_rtt:
        rtt_means, rtt_stds, rtt_vals = compute_stats(data, "rtt_ms")

        ax_line = fig.add_subplot(gs[row, 0])
        plot_line(ax_line, distances, rtt_means, rtt_stds,
                  "RTT (ms)", "RTT Mean vs Distance", "#d62728")

        ax_box = fig.add_subplot(gs[row, 1])
        plot_box(ax_box, distances, rtt_vals,
                 "RTT (ms)", "RTT Distribution per Step", "#d62728")

        row += 1

    if save:
        out = "{}.png".format(out_prefix)
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print("Saved {}".format(out))
    else:
        plt.show()


# ── Summary table ─────────────────────────────────────────────────────────────

def print_summary(data):
    distances = sorted(data["distance_m"].dropna().unique())
    header = "{:>10}  {:>10}  {:>8}  {:>10}  {:>8}  {:>6}".format(
        "Dist (m)", "RSSI mean", "RSSI std", "RTT mean", "RTT std", "n")
    print("\n" + header)
    print("-" * len(header))
    for d in distances:
        sub   = data[data["distance_m"] == d]
        rssi  = sub["rssi_dbm"].dropna()
        rtt   = sub["rtt_ms"].dropna()
        rssi_m = "{:.1f}".format(rssi.mean()) if len(rssi) else "—"
        rssi_s = "{:.1f}".format(rssi.std())  if len(rssi) > 1 else "—"
        rtt_m  = "{:.0f}".format(rtt.mean())  if len(rtt)  else "—"
        rtt_s  = "{:.0f}".format(rtt.std())   if len(rtt)  > 1 else "—"
        n      = len(sub)
        print("{:>10.2f}  {:>10}  {:>8}  {:>10}  {:>8}  {:>6}".format(
            d, rssi_m, rssi_s, rtt_m, rtt_s, n))
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyse range test CSV(s) and plot RSSI / RTT vs distance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "csvs", nargs="*",
        help="Path(s) to range_*.csv file(s). Omit to auto-find latest."
    )
    parser.add_argument(
        "--kind", choices=["DATA", "ACK"], default=None,
        help="Filter to a specific packet kind: DATA (forward-path) or ACK (reverse-path). Default: use all."
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Save plots as PNG instead of showing interactively."
    )
    args = parser.parse_args()

    if args.csvs:
        paths = args.csvs
    else:
        p = _latest_csv()
        if not p:
            print("No range_*.csv found in range_test_results/. Run range_test_logger.py first.")
            sys.exit(1)
        print("Auto-selected: {}".format(p))
        paths = [p]

    data = load_csvs(paths, kind_filter=args.kind)
    print_summary(data)
    make_figure(data, save=args.save)


if __name__ == "__main__":
    main()

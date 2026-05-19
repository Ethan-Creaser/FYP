#!/usr/bin/env python3
"""plot_topology.py — Visualise a topology JSON as a tiered network graph.

Nodes are arranged in columns (tiers) by BFS depth from a root node.
egg_99 (ground station / debug egg) is excluded from the graph.

Usage:
    python plot_topology.py topology.json
    python plot_topology.py topology.json --root 6        # BFS root = egg_6
    python plot_topology.py topology.json --highlight 2   # mark egg_2 in red
    python plot_topology.py topology.json --save out.png

Requires: pip install matplotlib networkx
"""

import argparse
import sys
from collections import defaultdict, deque

try:
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import networkx as nx
except ImportError:
    print("Required: pip install matplotlib networkx")
    sys.exit(1)

from topology import Topology

EXCLUDE_NODES = {99}   # ground station — never plotted


# ── Graph helpers ─────────────────────────────────────────────────────────────

def build_graph(topo: Topology) -> nx.Graph:
    G = nx.Graph()
    node_ids = [n for n in topo.node_ids() if n not in EXCLUDE_NODES]
    for nid in node_ids:
        G.add_node(nid)
    for nid in node_ids:
        for nb in topo.neighbours(nid):
            if nb in G and nb != nid:
                G.add_edge(nid, nb)
    return G


def bfs_tiers(G: nx.Graph, root: int) -> dict:
    """Return {node_id: tier} via BFS from root. Disconnected nodes go last."""
    tier = {root: 0}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for nb in G.neighbors(node):
            if nb not in tier:
                tier[nb] = tier[node] + 1
                queue.append(nb)
    max_tier = max(tier.values(), default=0)
    for node in G.nodes():
        if node not in tier:
            max_tier += 1
            tier[node] = max_tier
    return tier


def tiered_pos(tiers: dict) -> dict:
    """Map {node: tier} → {node: (x, y)} with nodes evenly spaced per column."""
    columns = defaultdict(list)
    for node, t in tiers.items():
        columns[t].append(node)

    pos = {}
    for t, nodes in columns.items():
        nodes_sorted = sorted(nodes)
        n = len(nodes_sorted)
        for i, node in enumerate(nodes_sorted):
            x = t * 3.0
            y = (i - (n - 1) / 2.0) * 2.0
            pos[node] = (x, y)
    return pos


def pick_root(G: nx.Graph) -> int:
    """Default root: node with the highest degree (most connections)."""
    return max(G.nodes(), key=lambda n: G.degree(n))


# ── Plot ──────────────────────────────────────────────────────────────────────

def plot(topo: Topology, root: int | None, highlight: list, save: str | None):
    G = build_graph(topo)
    if G.number_of_nodes() == 0:
        sys.exit("No nodes to plot (all excluded or empty topology).")

    chosen_root = root if root is not None else pick_root(G)
    if chosen_root not in G:
        sys.exit(f"egg_{chosen_root} not in graph (excluded or missing from topology).")

    tiers = bfs_tiers(G, chosen_root)
    pos   = tiered_pos(tiers)

    issues = topo.validate()
    asymmetric = set()
    for issue in issues:
        for nid in G.nodes():
            if f"egg_{nid}" in issue:
                asymmetric.add(nid)

    # Node colours
    colours = []
    for nid in G.nodes():
        if nid in highlight:
            colours.append("#e74c3c")   # red   — highlighted
        elif nid == chosen_root:
            colours.append("#2ecc71")   # green — root
        elif nid in asymmetric:
            colours.append("#e67e22")   # orange — asymmetric edge
        else:
            colours.append("#3498db")   # blue  — normal

    # Tier background bands
    n_tiers = max(tiers.values()) + 1
    fig, ax = plt.subplots(figsize=(max(9, n_tiers * 3 + 2), 7))

    for t in range(n_tiers):
        x_left  = t * 3.0 - 1.2
        x_right = t * 3.0 + 1.2
        band_col = "#f4f6f8" if t % 2 == 0 else "#eaecee"
        ax.axvspan(x_left, x_right, color=band_col, zorder=0)
        ax.text(t * 3.0, ax.get_ylim()[0] if ax.get_ylim()[0] != 0 else -0.5,
                f"Tier {t}", ha="center", va="top",
                fontsize=8, color="#aaaaaa", zorder=1)

    # Edges — differentiate by whether they cross tiers or stay within
    same_tier_edges = [(u, v) for u, v in G.edges() if tiers[u] == tiers[v]]
    cross_tier_edges = [(u, v) for u, v in G.edges() if tiers[u] != tiers[v]]

    nx.draw_networkx_edges(G, pos, edgelist=cross_tier_edges, ax=ax,
                           width=2.2, alpha=0.7, edge_color="#5d6d7e")
    nx.draw_networkx_edges(G, pos, edgelist=same_tier_edges, ax=ax,
                           width=1.5, alpha=0.5, edge_color="#aab7b8",
                           style="dashed")

    nc = nx.draw_networkx_nodes(G, pos, ax=ax, node_size=1100,
                                node_color=colours, alpha=0.95)
    nc.set_zorder(3)
    labels = {nid: f"egg_{nid}" for nid in G.nodes()}
    nx.draw_networkx_labels(G, pos, labels=labels, ax=ax,
                            font_size=8.5, font_color="white",
                            font_weight="bold")

    # Tier labels at top
    for t in range(n_tiers):
        nodes_in_tier = [n for n, d in tiers.items() if d == t]
        ax.text(t * 3.0, max(p[1] for p in pos.values()) + 1.0,
                f"Tier {t}", ha="center", va="bottom",
                fontsize=9, color="#666666", fontweight="bold")

    title = f"Mesh Topology  (root = egg_{chosen_root})"
    if highlight:
        hl_str = ", ".join(f"egg_{h}" for h in highlight)
        title += f"  |  red = {hl_str}"
    ax.set_title(title, fontsize=13, fontweight="bold", pad=16)
    ax.axis("off")

    # Legend
    legend_items = [
        mpatches.Patch(color="#2ecc71", label=f"egg_{chosen_root}  (root / tier 0)"),
        mpatches.Patch(color="#3498db", label="mesh node"),
    ]
    if highlight:
        legend_items.append(mpatches.Patch(color="#e74c3c", label="highlighted"))
    if asymmetric:
        legend_items.append(mpatches.Patch(color="#e67e22", label="asymmetric edge"))
    ax.legend(handles=legend_items, loc="lower right", fontsize=8,
              framealpha=0.85)

    # Info line
    info = (f"Nodes: {G.number_of_nodes()}   "
            f"Edges: {G.number_of_edges()}   "
            f"Tiers: {n_tiers}")
    if issues:
        info += f"   ⚠ {len(issues)} asymmetric edge(s)"
    ax.text(0.01, 0.01, info, transform=ax.transAxes,
            fontsize=8, color="#777777", va="bottom")

    plt.tight_layout()

    if save:
        plt.savefig(save, dpi=150, bbox_inches="tight")
        print(f"Saved to {save}")
    else:
        plt.show()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Plot topology JSON as a tiered network graph (egg_99 excluded).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python plot_topology.py topology.json
  python plot_topology.py topology.json --root 6
  python plot_topology.py topology.json --root 6 --highlight 2
  python plot_topology.py topology.json --save topology.png
        """,
    )
    ap.add_argument("topology", help="Topology JSON file")
    ap.add_argument("--root", type=int, metavar="ID",
                    help="Root node for tier layout (default: highest-degree node)")
    ap.add_argument("--highlight", type=int, nargs="+", metavar="ID",
                    help="Node ID(s) to highlight in red")
    ap.add_argument("--save", metavar="FILE",
                    help="Save to file instead of displaying (e.g. out.png)")
    args = ap.parse_args()

    try:
        topo = Topology.load(args.topology)
    except Exception as e:
        sys.exit(f"Cannot load topology: {e}")

    issues = topo.validate()
    if issues:
        print("Asymmetric edges:")
        for issue in issues:
            print(f"  {issue}")

    plot(topo, root=args.root, highlight=args.highlight or [], save=args.save)


if __name__ == "__main__":
    main()

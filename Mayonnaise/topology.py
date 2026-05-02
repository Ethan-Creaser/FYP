"""Topology loader for test-only artificial neighbour allowlists.

Expected file per-node: topology_<node_id>.json with content:
{ "allowed_neighbors": [2,3] }
"""

import os
import json
from typing import Optional, Set


def load_allowed_neighbors(node_id: int, folder: str = ".") -> Optional[Set[int]]:
    fn = os.path.join(folder, f"topology_{node_id}.json")
    if not os.path.exists(fn):
        return None
    with open(fn, "r", encoding="utf-8") as f:
        data = json.load(f)
    arr = data.get("allowed_neighbors") or []
    return set(int(x) for x in arr)


def load_all_topologies(folder: str = "."):
    """Return dict node_id -> set(allowed_neighbors) for all topology files found."""
    res = {}
    for name in os.listdir(folder):
        if not name.startswith("topology_") or not name.endswith(".json"):
            continue
        try:
            nid = int(name[len("topology_") : -len(".json")])
        except ValueError:
            continue
        res[nid] = load_allowed_neighbors(nid, folder)
    return res

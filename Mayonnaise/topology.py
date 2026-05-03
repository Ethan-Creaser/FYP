"""Topology loader for test-only artificial neighbour allowlists.

Expected file per-node: topology_<node_id>.json with content:
{ "allowed_neighbors": [2,3] }
"""

try:
    import uos as os
except ImportError:
    import os
import json


def load_allowed_neighbors(node_id, folder="."):
    fn = folder + "/topology_" + str(node_id) + ".json"
    try:
        with open(fn, "r") as f:
            data = json.load(f)
    except Exception:
        return None
    arr = data.get("allowed_neighbors") or []
    return set(int(x) for x in arr)


def load_all_topologies(folder="."):
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

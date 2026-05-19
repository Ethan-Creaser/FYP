"""topology.py — Canonical topology loader for PC scripts.

Single source of truth for reading, validating, and accessing topology files.

Canonical JSON format
---------------------
    {
        "egg_6":  {"uwb_id": 6, "neighbors": [7, 10]},
        "egg_7":  {"uwb_id": 7, "neighbors": [6, 8, 9]},
        "egg_10": {"uwb_id": 2, "neighbors": [9, 6]}
    }

    Key       : BLE advertisement name ("egg_<node_id>")
    uwb_id    : UWB slot ID (0–7); 0 = tag role, 1–7 = anchor role
    neighbors : node_ids this egg is allowed to hear from

Importing
---------
    from topology import Topology

    topo = Topology.load("topology.json")

    topo.node_ids()           # [6, 7, 8, 9, 10]  (sorted)
    topo.neighbours(6)        # [7, 10]
    topo.uwb_id(6)            # 6
    topo.ble_name(6)          # "egg_6"
    topo.entries()            # iterator of (ble_name, node_id, uwb_id, neighbors)
    topo.as_expected()        # {6: [7, 10], 7: [6, 8, 9], ...}  for TopologyCheck.verify()

    issues = topo.validate()  # [] if symmetric, list of strings otherwise
"""

import json


class Topology:
    """Parsed and validated topology.

    Constructed via Topology.load(path) rather than directly.
    """

    def __init__(self, data: dict):
        # data: {node_id (int): {"uwb_id": int, "neighbors": [int, ...]}}
        self._data = data

    # ── Loading ───────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str) -> "Topology":
        """Load from a topology JSON file.

        Raises FileNotFoundError if the file is missing, ValueError if the
        format is unrecognisable.
        """
        try:
            with open(path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Topology file not found: {path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {path}: {e}")

        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a JSON object at the top level")

        data = {}
        for key, spec in raw.items():
            try:
                node_id = int(str(key).removeprefix("egg_"))
            except ValueError:
                raise ValueError(
                    f"Unrecognisable key '{key}' — expected 'egg_<N>' or '<N>'"
                )
            if isinstance(spec, dict):
                uwb_id    = int(spec.get("uwb_id", node_id))
                neighbors = [int(n) for n in spec.get("neighbors", [])]
            elif isinstance(spec, list):
                uwb_id    = node_id
                neighbors = [int(n) for n in spec]
            else:
                raise ValueError(
                    f"Value for '{key}' must be an object or list, got {type(spec).__name__}"
                )
            data[node_id] = {"uwb_id": uwb_id, "neighbors": neighbors}

        return cls(data)

    # ── Accessors ─────────────────────────────────────────────────────────────

    def node_ids(self) -> list:
        """Sorted list of node IDs in the topology."""
        return sorted(self._data)

    def neighbours(self, node_id: int) -> list:
        """Neighbour list for node_id. Raises KeyError if node not in topology."""
        return self._data[node_id]["neighbors"]

    def uwb_id(self, node_id: int) -> int:
        """UWB slot ID for node_id. Raises KeyError if node not in topology."""
        return self._data[node_id]["uwb_id"]

    @staticmethod
    def ble_name(node_id: int) -> str:
        """BLE advertisement name for a node (e.g. egg_6)."""
        return f"egg_{node_id}"

    def entries(self):
        """Yield (ble_name, node_id, uwb_id, neighbors) for every node, sorted by node_id.

        Convenience iterator for bt_topology.py's write_direct / write_via_gateway.
        """
        for node_id in self.node_ids():
            d = self._data[node_id]
            yield (self.ble_name(node_id), node_id, d["uwb_id"], d["neighbors"])

    def as_expected(self) -> dict:
        """Return {node_id: [neighbour_ids]} for use with TopologyCheck.verify()."""
        return {nid: list(self._data[nid]["neighbors"]) for nid in self.node_ids()}

    # ── Validation ────────────────────────────────────────────────────────────

    def validate(self) -> list:
        """Check that every neighbour relationship is bidirectional.

        Returns a list of human-readable issue strings.  Empty list means the
        topology is fully symmetric.  Nodes referenced as neighbours but absent
        from the file are skipped (they can't be verified).
        """
        issues = []
        seen = set()
        for nid in self.node_ids():
            for other in self.neighbours(nid):
                pair = (min(nid, other), max(nid, other))
                if pair in seen or other not in self._data:
                    continue
                seen.add(pair)
                nid_sees_other   = other in self.neighbours(nid)
                other_sees_nid   = nid   in self.neighbours(other)
                if nid_sees_other and not other_sees_nid:
                    issues.append(
                        f"egg_{nid} lists egg_{other} but egg_{other} does not list egg_{nid}"
                    )
                elif other_sees_nid and not nid_sees_other:
                    issues.append(
                        f"egg_{other} lists egg_{nid} but egg_{nid} does not list egg_{other}"
                    )
        return issues

    def __repr__(self):
        return f"Topology({self.node_ids()})"

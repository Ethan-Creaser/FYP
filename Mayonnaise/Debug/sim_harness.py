"""In-process simulator for the mesh core.

Models LoRa's physical broadcast accurately: every transmission is delivered
to all topology neighbours, not just the intended recipient.  This matches
on-device behaviour and ensures neighbour tables stay current from any TX.
"""

import time

from node import Node
import topology


class SimNetwork:
    def __init__(self, topology_map):
        # topology_map: node_id -> set(allowed neighbour ids)
        self.topology = {k: (v or set()) for k, v in topology_map.items()}
        self.nodes = {}

    def register_node(self, node):
        self.nodes[node.node_id] = node
        node.network = self
        self.topology.setdefault(node.node_id, set())

    def deliver(self, packet_bytes, from_id):
        """Broadcast to all topology neighbours of from_id — like real LoRa."""
        for nid in self.topology.get(from_id, []):
            if nid in self.nodes:
                self.nodes[nid].receive_raw(packet_bytes, rssi=-70, snr=10)


def make_chain_topology(n):
    """Return a topology_map for a linear chain 1-2-3-...-n."""
    topo = {}
    for i in range(1, n + 1):
        neigh = set()
        if i - 1 >= 1:
            neigh.add(i - 1)
        if i + 1 <= n:
            neigh.add(i + 1)
        topo[i] = neigh
    return topo


def main():
    # 4-node chain: 1 -- 2 -- 3 -- 4
    # Node 1 has no route to node 4 on start, so RREQ/RREP fires first.
    topo_map = make_chain_topology(4)
    net = SimNetwork(topo_map)

    nodes = {}
    for nid in topo_map:
        n = Node(nid, allowlist=topo_map[nid])
        net.register_node(n)
        nodes[nid] = n

    print("=== sim: Node 1 -> Node 4 (triggers RREQ/RREP then DATA) ===")
    nodes[1].send_data(dst=4, app_id=constants_app_localise(), subtype=1, data=b"hello from 1")

    time.sleep(0.1)

    print("=== sim: Node 4 -> Node 1 (route already cached from RREP) ===")
    nodes[4].send_data(dst=1, app_id=1, subtype=2, data=b"reply from 4")

    time.sleep(0.1)
    print("=== sim done ===")


def constants_app_localise():
    import constants
    return constants.APP_LOCALISE


if __name__ == "__main__":
    main()

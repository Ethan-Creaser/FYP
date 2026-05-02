"""Simple in-process simulator for the mesh core to validate packet flow and forwarding."""

import time
from typing import Dict, Optional

from node import Node
import topology


class SimNetwork:
    def __init__(self, topology_map: Dict[int, Optional[set]]):
        # topology_map: node_id -> set(allowed neighbour ids) or None
        # interpret None as broadcast-to-all (not used in tests)
        self.topology = {k: (v or set()) for k, v in topology_map.items()}
        self.nodes: Dict[int, Node] = {}

    def register_node(self, node: Node):
        self.nodes[node.node_id] = node
        node.network = self
        # ensure the node's topology exists
        self.topology.setdefault(node.node_id, set())

    def deliver(self, packet_bytes: bytes, from_id: int):
        # broadcast to all neighbours of from_id
        for nid in self.topology.get(from_id, []):
            if nid in self.nodes:
                self.nodes[nid].receive_raw(packet_bytes, from_id=from_id, rssi=-70, snr=10)

    def send_direct(self, packet_bytes: bytes, from_id: int, to_id: int):
        # deliver only to the single neighbour
        if to_id in self.nodes and to_id in self.topology.get(from_id, []):
            self.nodes[to_id].receive_raw(packet_bytes, from_id=from_id, rssi=-60, snr=12)


def make_chain_topology(n: int):
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
    # small chain 1-2-3-4 for multihop test
    topo_map = make_chain_topology(4)
    net = SimNetwork(topo_map)

    # create nodes and register
    nodes = {}
    for nid in topo_map:
        n = Node(nid, allowlist=topo_map[nid])
        net.register_node(n)
        nodes[nid] = n

    # Node 1 sends data to Node 4
    print("--- Starting sim: Node 1 -> Node 4 ---")
    nodes[1].send_data(dst=4, app_id=1, subtype=1, data=b"hello from 1")

    # Give the simulator a short moment for async-like propagation
    time.sleep(0.2)

    # Let Node 4 reply back to Node 1
    print("--- Node 4 replying back to Node 1 ---")
    nodes[4].send_data(dst=1, app_id=1, subtype=2, data=b"reply from 4")

    time.sleep(0.2)
    print("--- sim done ---")


if __name__ == "__main__":
    main()

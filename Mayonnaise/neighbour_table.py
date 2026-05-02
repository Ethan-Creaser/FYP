"""Simple neighbour table implementation used by the node state machine."""

import time
# avoid 'typing' imports for MicroPython
import constants


class NeighbourEntry:
    def __init__(self, node_id: int):
        self.node_id = node_id
        self.last_seen = time.time()
        self.rssi = None
        self.snr = None
        self.link_success_rate = 0
        self.hops_to_ground = None
        self.is_alive = True

    def touch(self, rssi: Optional[int] = None, snr: Optional[int] = None, hops_to_ground: Optional[int] = None):
        self.last_seen = time.time()
        if rssi is not None:
            self.rssi = rssi
        if snr is not None:
            self.snr = snr
        if hops_to_ground is not None:
            self.hops_to_ground = hops_to_ground
        self.is_alive = True

    def age_seconds(self) -> float:
        return time.time() - self.last_seen


class NeighbourTable:
    def __init__(self, allowlist=None):
        self._entries = {}
        self.allowlist = set(allowlist) if allowlist else None

    def update(self, node_id: int, rssi: Optional[int] = None, snr: Optional[int] = None, hops_to_ground: Optional[int] = None):
        if self.allowlist is not None and node_id not in self.allowlist:
            # ignore links not in allowlist when testing
            return
        e = self._entries.get(node_id)
        if e is None:
            e = NeighbourEntry(node_id)
            self._entries[node_id] = e
        e.touch(rssi=rssi, snr=snr, hops_to_ground=hops_to_ground)

    def mark_lost(self, node_id: int):
        e = self._entries.get(node_id)
        if e:
            e.is_alive = False

    def get_alive(self):
        now = time.time()
        res = []
        for nid, e in list(self._entries.items()):
            age = now - e.last_seen
            if age > constants.LOST_TIMEOUT:
                e.is_alive = False
            if e.is_alive:
                res.append(e)
        return res

    def get(self, node_id: int) -> Optional[NeighbourEntry]:
        return self._entries.get(node_id)

    def prune_stale(self):
        now = time.time()
        for nid, e in list(self._entries.items()):
            if now - e.last_seen > constants.LOST_TIMEOUT:
                del self._entries[nid]

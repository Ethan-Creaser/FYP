"""Simple neighbour table implementation used by the node state machine."""

import time
# avoid 'typing' imports for MicroPython
import constants


class NeighbourEntry:
    def __init__(self, node_id):
        self.node_id = node_id
        self.last_seen = time.time()
        self.rssi = None
        self.snr = None
        self.link_success_rate = 0
        self.hops_to_ground = None
        self.is_alive = True

    def touch(self, rssi=None, snr=None, hops_to_ground=None):
        self.last_seen = time.time()
        if rssi is not None:
            self.rssi = rssi
        if snr is not None:
            self.snr = snr
        if hops_to_ground is not None:
            self.hops_to_ground = hops_to_ground
        self.is_alive = True

    def age_seconds(self):
        return time.time() - self.last_seen


class NeighbourTable:
    def __init__(self, allowlist=None):
        self._entries = {}
        self.allowlist = set(allowlist) if allowlist else None

    def is_allowed(self, node_id):
        return self.allowlist is None or node_id in self.allowlist

    def update(self, node_id, rssi=None, snr=None, hops_to_ground=None):
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

    def get(self, node_id):
        return self._entries.get(node_id)

    def get_newly_lost(self):
        """Return node_ids that just transitioned alive->lost and mark them lost."""
        now = time.time()
        lost = []
        for nid, e in list(self._entries.items()):
            if e.is_alive and (now - e.last_seen) > constants.LOST_TIMEOUT:
                e.is_alive = False
                lost.append(nid)
        return lost

    def prune_stale(self):
        now = time.time()
        for nid, e in list(self._entries.items()):
            if now - e.last_seen > constants.LOST_TIMEOUT:
                del self._entries[nid]

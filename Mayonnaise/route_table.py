"""Simple route cache for next-hop routes."""

import time
# avoid typing imports for MicroPython
import constants


class RouteEntry:
    def __init__(self, dest: int, next_hop: int, hops: int):
        self.dest = dest
        self.next_hop = next_hop
        self.hops = hops
        self.last_used = time.time()
        self.failures = 0

    def touch(self):
        self.last_used = time.time()
        self.failures = 0


class RouteTable:
    def __init__(self):
        self._routes = {}

    def set_route(self, dest: int, next_hop: int, hops: int):
        e = RouteEntry(dest, next_hop, hops)
        self._routes[dest] = e

    def get_next_hop(self, dest: int) -> Optional[int]:
        e = self._routes.get(dest)
        if not e:
            return None
        # expire after DEFAULT_ROUTE_TTL_MS
        if (time.time() - e.last_used) * 1000 > constants.DEFAULT_ROUTE_TTL_MS:
            del self._routes[dest]
            return None
        e.touch()
        return e.next_hop

    def penalize(self, dest: int):
        e = self._routes.get(dest)
        if not e:
            return
        e.failures += 1
        if e.failures >= 3:
            del self._routes[dest]

    def invalidate_next_hop(self, next_hop: int):
        # remove any routes that use this next_hop
        for d, e in list(self._routes.items()):
            if e.next_hop == next_hop:
                del self._routes[d]

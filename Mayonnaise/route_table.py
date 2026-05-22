"""Simple route cache for next-hop routes."""

import constants


class RouteEntry:
    def __init__(self, dest, next_hop, hops):
        self.dest = dest
        self.next_hop = next_hop
        self.hops = hops
        self.failures = 0

    def touch(self):
        self.failures = 0


class RouteTable:
    def __init__(self):
        self._routes = {}

    def set_route(self, dest: int, next_hop: int, hops: int):
        e = RouteEntry(dest, next_hop, hops)
        self._routes[dest] = e

    def get_next_hop(self, dest):
        e = self._routes.get(dest)
        if not e:
            return None
        e.touch()
        return e.next_hop

    def penalize(self, dest: int):
        if dest == constants.GROUND_STATION_ID:
            return
        e = self._routes.get(dest)
        if not e:
            return
        e.failures += 1
        if e.failures >= 3:
            del self._routes[dest]

    def all_routes(self):
        """Return list of (dst, next_hop, hops) for every known route."""
        return [(e.dest, e.next_hop, e.hops) for e in self._routes.values()]

    def invalidate_next_hop(self, next_hop: int):
        for d, e in list(self._routes.items()):
            if e.next_hop == next_hop and e.dest != constants.GROUND_STATION_ID:
                del self._routes[d]

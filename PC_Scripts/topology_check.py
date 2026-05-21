#!/usr/bin/env python3
"""topology_check.py — Verify the mesh's actual neighbour topology via the debug egg (node 99).

Connects to egg_99 over BLE, broadcasts a CTRL_GET_NEIGHBOURS query, collects each
egg's NEIGHBOURS_REPORT response, and compares against an expected topology.

Importing in another test script
---------------------------------
    from topology_check import TopologyCheck

    async def my_test():
        async with TopologyCheck.connect() as topo:
            if not await topo.verify(expected):
                raise RuntimeError("topology mismatch — aborting test")
            # proceed with test ...

    # expected: dict mapping node_id (int) to list of expected alive neighbour ids
    expected = {1: [2, 3], 2: [1, 3], 3: [1, 2]}

Or, if you already hold a BleakClient:
    async with BleakClient(device) as client:
        topo = TopologyCheck(client)
        await topo.setup()
        ok = await topo.verify(expected)

Expected topology JSON format (canonical — same file used by bt_topology.py):
    { "egg_6": {"uwb_id": 6, "neighbors": [7, 10]}, ... }

CLI usage:
    python3 topology_check.py --compare topology.json
    python3 topology_check.py --query all
    python3 topology_check.py --query 3
    python3 topology_check.py                   # interactive shell
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import sys
import time

from topology import Topology

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak is required:  pip install bleak")
    sys.exit(1)

NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # egg → PC (notify)
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # PC → egg (write)

BT_CMD_GET_NEIGHBOURS = 0xD4
BT_CMD_GET_ROUTES     = 0xD5
BT_CMD_RESET_STATE    = 0xD7
BT_CMD_RESET_STATE    = 0xD7
DEBUG_EGG_NAME        = "egg_99"
DEBUG_EGG_ID          = 99
DEFAULT_TIMEOUT       = 15.0   # seconds to wait for reports after a broadcast

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"


class TopologyCheck:
    """Query the mesh neighbour tables via egg_99 and verify against an expected topology.

    Construct with an already-connected BleakClient and call setup(), or use the
    connect() classmethod as an async context manager to handle both.

    Typical usage in another test script::

        async with TopologyCheck.connect() as topo:
            ok = await topo.verify(expected)
            if not ok:
                sys.exit("Topology mismatch")

    Attributes:
        reports: dict mapping node_id -> list of alive neighbour ids, populated
                 after a query + collect cycle. Reset by verify() / query_all().
    """

    def __init__(self, client: BleakClient, timeout: float = DEFAULT_TIMEOUT, verbose: bool = True, debug: bool = False):
        self.client        = client
        self.timeout       = timeout
        self.verbose       = verbose
        self.debug         = debug
        self.reports: dict[int, list[int]] = {}
        self.route_reports: dict[int, dict[int, int]] = {}   # node_id -> {dst: next_hop}
        self._buf              = ""   # accumulates partial lines across BLE 20-byte chunks
        self._beacon_first_seen: dict[int, float] = {}   # node_id -> monotonic time
        self._formation_reports: dict[int, float] = {}   # node_id -> formation time (s)

    # ── Connection helper ─────────────────────────────────────────────────────

    @classmethod
    @asynccontextmanager
    async def connect(cls, name: str = DEBUG_EGG_NAME, timeout: float = DEFAULT_TIMEOUT, verbose: bool = True, debug: bool = False):
        """Async context manager: scan for the debug egg, connect, and yield a ready instance.

        Example::

            async with TopologyCheck.connect() as topo:
                ok = await topo.verify(expected)
        """
        if verbose:
            print(f"Scanning for {name}...")
        device = await BleakScanner.find_device_by_name(name, timeout=10)
        if device is None:
            raise RuntimeError(f"Could not find {name} — is egg_99 powered on and in range?")
        if verbose:
            print(f"Found {name} at {device.address}")
        async with BleakClient(device) as client:
            instance = cls(client, timeout=timeout, verbose=verbose, debug=debug)
            await instance.setup()
            yield instance

    # ── Setup ─────────────────────────────────────────────────────────────────

    async def setup(self):
        """Subscribe to BLE notifications. Must be called once after connecting."""
        await self.client.start_notify(NUS_TX_UUID, self._on_notify)
        await asyncio.sleep(0.5)   # let the notify subscription settle

    # ── High-level API ────────────────────────────────────────────────────────

    async def verify(self, expected: dict[int, list[int]], timeout: float | None = None) -> bool:
        """Unicast a query to each expected node, collect responses, compare, and print results.

        This is the one-call API for use at the top of a test script.

        Args:
            expected: mapping of node_id -> list of expected alive neighbour ids.
            timeout:  seconds to wait for responses (uses instance default if omitted).

        Returns:
            True if every node in expected reports exactly the expected neighbours.
        """
        self.reports.clear()
        await self.query_all(node_ids=list(expected.keys()))
        await self.collect(expected_ids=list(expected.keys()),
                           timeout=timeout or self.timeout)
        return self.compare(expected)

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query_all(self, node_ids: list[int] | None = None):
        """Unicast CTRL_GET_NEIGHBOURS to each egg in node_ids.

        If node_ids is None, falls back to a broadcast (useful for ad-hoc discovery
        in the interactive shell when the topology is unknown).
        """
        if node_ids is None:
            if self.verbose:
                print("Querying all eggs for active neighbours (broadcast)...")
            await self._send(bytes([BT_CMD_GET_NEIGHBOURS, 0xFF]))
        else:
            for nid in node_ids:
                await self.query_one(nid)
                # Wait for this egg's unicast DATA response before querying the next
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if nid in self.reports:
                        break
                    await asyncio.sleep(0.1)

    async def query_one(self, node_id: int):
        """Send CTRL_GET_NEIGHBOURS to a single egg (unicast via egg_99)."""
        if self.verbose:
            print(f"Querying egg_{node_id} for active neighbours...")
        await self._send(bytes([BT_CMD_GET_NEIGHBOURS, node_id & 0xFF]))

    async def query_routes_all(self, node_ids: list[int] | None = None):
        """Unicast CTRL_GET_ROUTES to each egg in node_ids.

        Falls back to broadcast if node_ids is None.
        """
        if node_ids is None:
            if self.verbose:
                print("Querying all eggs for route tables (broadcast)...")
            await self._send(bytes([BT_CMD_GET_ROUTES, 0xFF]))
        else:
            for nid in node_ids:
                await self.query_routes_one(nid)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if nid in self.route_reports:
                        break
                    await asyncio.sleep(0.1)


    async def query_routes_one(self, node_id: int):
        """Send CTRL_GET_ROUTES to a single egg (unicast via egg_99)."""
        if self.verbose:
            print(f"Querying egg_{node_id} for route table...")
        await self._send(bytes([BT_CMD_GET_ROUTES, node_id & 0xFF]))

    async def collect_routes(self, expected_ids: list[int] | None = None,
                             timeout: float | None = None) -> dict[int, dict[int, int]]:
        """Wait for ROUTES_REPORT lines and return a snapshot of self.route_reports."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            if expected_ids and set(expected_ids).issubset(self.route_reports):
                break
        return dict(self.route_reports)

    def trace_path(self, src: int, dst: int) -> list[int]:
        """Reconstruct the forwarding path from src to dst using collected route tables.

        Returns the ordered list of node IDs from src to dst (inclusive), or an
        empty list if the path cannot be determined from the gathered route data.
        """
        path = [src]
        visited = {src}
        current = src
        while current != dst:
            routes = self.route_reports.get(current)
            if routes is None:
                print(f"  [trace] no route data for egg_{current}")
                return []
            next_hop = routes.get(dst)
            if next_hop is None:
                print(f"  [trace] egg_{current} has no route to egg_{dst}")
                return []
            if next_hop in visited:
                print(f"  [trace] loop detected at egg_{next_hop}")
                return []
            path.append(next_hop)
            visited.add(next_hop)
            current = next_hop
        return path

    def print_routing_subgraph(self):
        """Print the routing subgraph reconstructed from all route_reports."""
        print(f"\n{_BOLD}{'='*52}{_RESET}")
        print(f"{_BOLD}  ROUTING SUBGRAPH{_RESET}")
        print(f"{_BOLD}{'='*52}{_RESET}")
        if not self.route_reports:
            print("  (no route reports collected)")
        for node_id in sorted(self.route_reports):
            routes = self.route_reports[node_id]
            if routes:
                pairs = "  ".join(f"{dst}→{nh}" for dst, nh in sorted(routes.items()))
                print(f"  egg_{node_id:<3}  {pairs}")
            else:
                print(f"  egg_{node_id:<3}  (empty route table)")
        print(f"{_BOLD}{'='*52}{_RESET}\n")

    # ── Collect ───────────────────────────────────────────────────────────────

    async def collect(self, expected_ids: list[int] | None = None,
                      timeout: float | None = None) -> dict[int, list[int]]:
        """Wait for NEIGHBOURS_REPORT lines to arrive.

        Returns early once all expected_ids have reported (or timeout expires).
        Returns a snapshot of self.reports.
        """
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            if expected_ids and set(expected_ids).issubset(self.reports):
                break
        return dict(self.reports)

    # ── Compare ───────────────────────────────────────────────────────────────

    def compare(self, expected: dict[int, list[int]]) -> bool:
        """Compare self.reports against expected and print a formatted summary.

        Returns True if every node matches exactly (no missing, no extra neighbours).
        Eggs that responded but are absent from expected are flagged as unexpected.
        The debug egg (node 99) is always excluded from both sides.
        """
        expected = {k: v for k, v in expected.items() if k != DEBUG_EGG_ID}
        reports  = {k: v for k, v in self.reports.items() if k != DEBUG_EGG_ID}

        print(f"\n{_BOLD}{'='*52}{_RESET}")
        print(f"{_BOLD}  TOPOLOGY CHECK{_RESET}")
        print(f"{_BOLD}{'='*52}{_RESET}")

        all_ok = True

        for node_id in sorted(expected):
            expected_set = set(expected[node_id])
            actual = reports.get(node_id)

            if actual is None:
                print(f"  egg_{node_id:<3}  {_RED}NO REPORT{_RESET}")
                all_ok = False
                continue

            actual_set = set(actual)
            missing = expected_set - actual_set
            extra   = actual_set - expected_set

            if not missing and not extra:
                print(f"  egg_{node_id:<3}  {_GREEN}OK{_RESET}  {sorted(actual_set)}")
            else:
                all_ok = False
                print(f"  egg_{node_id:<3}  {_RED}MISMATCH{_RESET}")
                if missing:
                    print(f"           {_YELLOW}MISSING{_RESET} {sorted(missing)}")
                if extra:
                    print(f"           {_YELLOW}EXTRA  {_RESET} {sorted(extra)}")

        for node_id in sorted(set(reports) - set(expected)):
            print(f"  egg_{node_id:<3}  {_YELLOW}UNEXPECTED{_RESET}  "
                  f"sees {sorted(reports[node_id])}")

        print(f"{_BOLD}{'='*52}{_RESET}")
        label = f"{_GREEN}PASS{_RESET}" if all_ok else f"{_RED}FAIL{_RESET}"
        print(f"  Result: {_BOLD}{label}{_RESET}\n")
        return all_ok

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _send(self, data: bytes):
        await self.client.write_gatt_char(NUS_RX_UUID, data, response=True)

    def _on_notify(self, _, data: bytearray):
        try:
            self._buf += data.decode("utf-8", errors="replace")
        except Exception:
            return
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            if self.debug:
                print(f"  egg_99: {line}")
            if line.startswith("NEIGHBOURS_REPORT "):
                self._parse_report(line)
            elif line.startswith("ROUTES_REPORT "):
                self._parse_routes_report(line)
            elif " BEACON from=" in line:
                self._parse_beacon(line)
            elif line.startswith("FORMATION_REPORT "):
                self._parse_formation_report(line)

    def _parse_report(self, line: str):
        # NEIGHBOURS_REPORT node=3 alive=6,7,8
        kv = {}
        for tok in line.split()[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k] = v
        try:
            node = int(kv["node"])
        except (KeyError, ValueError):
            print(f"  [warn] malformed report: {line}")
            return
        alive_str = kv.get("alive", "")
        neighbours = [int(x) for x in alive_str.split(",") if x.strip()] if alive_str else []
        self.reports[node] = neighbours
        print(f"  {_CYAN}REPORT{_RESET}  egg_{node} sees {sorted(neighbours)}")

    def _parse_routes_report(self, line: str):
        # ROUTES_REPORT node=3 routes=6->7 8->9
        parts = line.split(None, 2)   # ["ROUTES_REPORT", "node=3", "routes=6->7 8->9"]
        if len(parts) < 2:
            return
        try:
            node = int(parts[1].split("=", 1)[1])
        except (IndexError, ValueError):
            print(f"  [warn] malformed routes report: {line}")
            return
        routes: dict[int, int] = {}
        if len(parts) == 3:
            route_str = parts[2]
            if route_str.startswith("routes="):
                route_str = route_str[len("routes="):]
            for pair in route_str.split():
                if "->" in pair:
                    try:
                        d, nh = pair.split("->", 1)
                        routes[int(d)] = int(nh)
                    except ValueError:
                        pass
        self.route_reports[node] = routes
        pairs_str = "  ".join(f"{d}→{nh}" for d, nh in sorted(routes.items())) or "(empty)"
        print(f"  {_CYAN}ROUTES{_RESET}  egg_{node}: {pairs_str}")

    def _parse_formation_report(self, line: str):
        # FORMATION_REPORT node=1 time=4.2s
        try:
            kv = {}
            for tok in line.split()[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k] = v.rstrip("s")
            node_id = int(kv["node"])
            ft_s    = float(kv["time"])
            self._formation_reports[node_id] = ft_s
            print(f"  {_GREEN}FORMED{_RESET}  egg_{node_id} fully connected at {ft_s:.1f}s after boot")
        except (KeyError, ValueError):
            pass

    async def collect_formation(self, expected_ids: list, timeout: float = 120.0) -> tuple:
        """Wait for FORMATION_REPORT from all expected nodes.
        Returns (reports_dict, all_reported_bool)."""
        self._formation_reports.clear()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if set(expected_ids).issubset(self._formation_reports):
                break
        ok = set(expected_ids).issubset(self._formation_reports)
        return dict(self._formation_reports), ok

    def _parse_beacon(self, line: str):
        # "[99] BEACON from=1 hops_to_ground=1"
        try:
            for tok in line.split():
                if tok.startswith("from="):
                    node_id = int(tok.split("=", 1)[1])
                    if node_id not in self._beacon_first_seen:
                        self._beacon_first_seen[node_id] = time.monotonic()
                    return
        except (ValueError, IndexError):
            pass

    async def reset_all(self):
        """Send CTRL_RESET_STATE broadcast via egg_99 to wipe mesh state on all nodes."""
        if self.verbose:
            print("Broadcasting mesh state reset to all nodes...")
        await self._send(bytes([BT_CMD_RESET_STATE]))

    async def reset_all(self):
        """Broadcast CTRL_RESET_STATE via egg_99 to synchronously wipe mesh state."""
        if self.verbose:
            print("Broadcasting mesh state reset to all nodes...")
        await self._send(bytes([BT_CMD_RESET_STATE]))

    async def watch_formation(self, expected_ids: list, timeout: float = 120.0, clear: bool = True) -> tuple:
        """Listen for beacons from expected_ids and record when each first appears.

        Returns (formation_time_s, seen_set).
        formation_time_s is the span from the first beacon heard to the last.
        Prints a live join event for each node as it appears.
        """
        if clear:
            self._beacon_first_seen.clear()
        deadline = time.monotonic() + timeout
        notified: set = set()
        t_first = None

        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            for nid in sorted((set(self._beacon_first_seen) & set(expected_ids)) - notified):
                t = self._beacon_first_seen[nid]
                if t_first is None:
                    t_first = t
                print(f"  {_GREEN}✓{_RESET}  egg_{nid} heard  (+{t - t_first:.1f}s)")
                notified.add(nid)
            if set(expected_ids).issubset(notified):
                break

        seen = set(self._beacon_first_seen) & set(expected_ids)
        times = [self._beacon_first_seen[n] for n in seen]
        ft = (max(times) - min(times)) if len(times) >= 2 else (0.0 if times else None)
        return ft, seen


# ── CLI ───────────────────────────────────────────────────────────────────────

async def _cli_compare(topo: TopologyCheck, path: str):
    await topo.verify(Topology.load(path).as_expected())


async def _cli_query(topo: TopologyCheck, target_id: int, timeout: float):
    topo.reports.clear()
    if target_id == 0xFF:
        await topo.query_all()
    else:
        await topo.query_one(target_id)
    await asyncio.sleep(timeout)


async def _cli_routes(topo: TopologyCheck, target_id: int, timeout: float):
    topo.route_reports.clear()
    if target_id == 0xFF:
        await topo.query_routes_all()   # broadcast fallback for ad-hoc use
    else:
        await topo.query_routes_one(target_id)
    await topo.collect_routes(timeout=timeout)
    topo.print_routing_subgraph()


async def _cli_interactive(topo: TopologyCheck):
    print("Commands:  neighbours [<id>|all]   routes [<id>|all]   "
          "trace <src> <dst>   compare <file.json>   quit")
    loop = asyncio.get_event_loop()
    while True:
        try:
            raw = await loop.run_in_executor(None, input, "> ")
        except EOFError:
            break
        parts = raw.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()
        if cmd == "quit":
            break
        elif cmd in ("neighbours", "n"):
            arg = parts[1] if len(parts) > 1 else "all"
            target = 0xFF if arg == "all" else int(arg)
            topo.reports.clear()
            await _cli_query(topo, target, topo.timeout)
        elif cmd in ("routes", "r"):
            arg = parts[1] if len(parts) > 1 else "all"
            target = 0xFF if arg == "all" else int(arg)
            await _cli_routes(topo, target, topo.timeout)
        elif cmd == "trace":
            if len(parts) < 3:
                print("Usage: trace <src_id> <dst_id>")
                continue
            try:
                src, dst = int(parts[1]), int(parts[2])
                path = topo.trace_path(src, dst)
                if path:
                    print(f"  Path: {' -> '.join(f'egg_{n}' for n in path)}")
                else:
                    print("  Path could not be determined (collect routes first)")
            except ValueError:
                print("trace: IDs must be integers")
        elif cmd == "compare":
            if len(parts) < 2:
                print("Usage: compare <topology.json>")
                continue
            try:
                await _cli_compare(topo, parts[1])
            except Exception as e:
                print(f"Error loading topology: {e}")
        else:
            print(f"Unknown: {cmd}")


async def _run(args):
    async with TopologyCheck.connect(timeout=args.timeout) as topo:
        if args.compare:
            await _cli_compare(topo, args.compare)
        elif args.query is not None:
            await _cli_query(topo, args.query, args.timeout)
        elif args.routes is not None:
            await _cli_routes(topo, args.routes, args.timeout)
        else:
            await _cli_interactive(topo)


def _parse_target(s: str) -> int:
    return 0xFF if s.lower() == "all" else int(s)


def main():
    parser = argparse.ArgumentParser(
        description="Verify mesh neighbour topology via the debug egg (node 99)"
    )
    parser.add_argument("--compare", metavar="FILE",
                        help="Compare actual topology against this JSON file")
    parser.add_argument("--query", metavar="ID|all", type=_parse_target,
                        help="Query a specific egg ID or 'all' for broadcast")
    parser.add_argument("--routes", metavar="ID|all", type=_parse_target,
                        help="Dump route tables from a specific egg or 'all' (broadcast)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Seconds to wait for reports (default: {DEFAULT_TIMEOUT})")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

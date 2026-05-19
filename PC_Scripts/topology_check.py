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

    def __init__(self, client: BleakClient, timeout: float = DEFAULT_TIMEOUT):
        self.client  = client
        self.timeout = timeout
        self.reports: dict[int, list[int]] = {}
        self._buf    = ""   # accumulates partial lines across BLE 20-byte chunks

    # ── Connection helper ─────────────────────────────────────────────────────

    @classmethod
    @asynccontextmanager
    async def connect(cls, name: str = DEBUG_EGG_NAME, timeout: float = DEFAULT_TIMEOUT):
        """Async context manager: scan for the debug egg, connect, and yield a ready instance.

        Example::

            async with TopologyCheck.connect() as topo:
                ok = await topo.verify(expected)
        """
        print(f"Scanning for {name}...")
        device = await BleakScanner.find_device_by_name(name, timeout=10)
        if device is None:
            raise RuntimeError(f"Could not find {name} — is egg_99 powered on and in range?")
        print(f"Found {name} at {device.address}")
        async with BleakClient(device) as client:
            instance = cls(client, timeout=timeout)
            await instance.setup()
            yield instance

    # ── Setup ─────────────────────────────────────────────────────────────────

    async def setup(self):
        """Subscribe to BLE notifications. Must be called once after connecting."""
        await self.client.start_notify(NUS_TX_UUID, self._on_notify)
        await asyncio.sleep(0.5)   # let the notify subscription settle

    # ── High-level API ────────────────────────────────────────────────────────

    async def verify(self, expected: dict[int, list[int]], timeout: float | None = None) -> bool:
        """Broadcast a query, collect responses, compare, and print results.

        This is the one-call API for use at the top of a test script.

        Args:
            expected: mapping of node_id -> list of expected alive neighbour ids.
            timeout:  seconds to wait for responses (uses instance default if omitted).

        Returns:
            True if every node in expected reports exactly the expected neighbours.
        """
        self.reports.clear()
        await self.query_all()
        await self.collect(expected_ids=list(expected.keys()),
                           timeout=timeout or self.timeout)
        return self.compare(expected)

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query_all(self):
        """Broadcast CTRL_GET_NEIGHBOURS — every egg in the mesh responds."""
        print("Querying all eggs for active neighbours (broadcast)...")
        await self._send(bytes([BT_CMD_GET_NEIGHBOURS, 0xFF]))

    async def query_one(self, node_id: int):
        """Send CTRL_GET_NEIGHBOURS to a single egg."""
        print(f"Querying egg_{node_id} for active neighbours...")
        await self._send(bytes([BT_CMD_GET_NEIGHBOURS, node_id & 0xFF]))

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
            print(f"  egg_99: {line}")
            if line.startswith("NEIGHBOURS_REPORT "):
                self._parse_report(line)

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


async def _cli_interactive(topo: TopologyCheck):
    print("Commands:  neighbours [<id>|all]   compare <file.json>   quit")
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
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Seconds to wait for reports (default: {DEFAULT_TIMEOUT})")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

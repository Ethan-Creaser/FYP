#!/usr/bin/env python3
"""test_1a_mesh_check.py — Checklist 1 / Test 1a: Well-Connected Mesh

Automates the setup verification and multi-path rerouting test.  Connects to
the debug egg (egg_99) for topology checks and to the source egg for
ping-based PDR measurements.

Phases
------
  1. Setup & formation  — topology verified via egg_99, formation time recorded
  2. PDR baseline       — A → C ping burst with no failure
  3. Rerouting (×N)     — pre-kill / transition / post-reroute batches;
                          reroute time, auto-reroute flag, rejoin time
  4. Summary report + CSV saved to test_results/

Usage
-----
  python test_1a_mesh_check.py topology.json --source 1 --dest 3 --via 2
  python test_1a_mesh_check.py topology.json \\
      --source 1 --dest 3 --via 2 --trials 5 --pings 20

Arguments
---------
  topology             : topology JSON file (canonical bt_topology.py format)
  --source ID          : node ID of the source egg (A)
  --dest   ID          : node ID of the destination egg (C)
  --via    ID          : node ID of the intermediate egg to kill (B)
  --trials N           : rerouting trial count (default 5)
  --pings  N           : pings per measurement batch (default 20)
  --formation-timeout  : seconds to wait for topology verification (default 30)

Requires: pip install bleak
"""

import argparse
import asyncio
import csv
import datetime
import os
import re
import sys
import time as _time
from contextlib import asynccontextmanager

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak is required:  pip install bleak")
    sys.exit(1)

from topology import Topology
from topology_check import TopologyCheck

NUS_RX_UUID  = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_UUID  = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
CMD_PING     = 0xD3
SCAN_TIMEOUT = 10.0

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"

_ACK_RE    = re.compile(r'\[\d+\] ACK confirmed seq=(\d+) from=(\d+).*rtt_ms=(\d+)')
_SEND_RE   = re.compile(r'\[(\d+)\] SEND dst=(\d+) seq=(\d+).*next_hop=(\S+)')
_DONE_RE   = re.compile(r'^PING_DONE')
_START_RE  = re.compile(r'^PING_START')
_WAIT_RE   = re.compile(r'^PING_WAIT')
_ROUTE_RE  = re.compile(r'^PING_ROUTE')
_RREP_RE    = re.compile(r'RREP: route\((\d+)\) via (\d+) hops=(\d+)')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _col(pdr: float) -> str:
    return _GREEN if pdr >= 0.9 else (_YELLOW if pdr >= 0.5 else _RED)


async def _ask(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, prompt)


def all_paths(topo: Topology, src: int, dst: int) -> list:
    """Return all simple paths from src to dst using declared topology edges."""
    graph = {n: set(topo.neighbours(n)) for n in topo.node_ids()}
    found = []

    def dfs(node, path, seen):
        if node == dst:
            found.append(list(path))
            return
        for nb in sorted(graph.get(node, [])):
            if nb not in seen and nb in graph:
                seen.add(nb)
                dfs(nb, path + [nb], seen)
                seen.remove(nb)

    dfs(src, [src], {src})
    return found


# ── PingSession ───────────────────────────────────────────────────────────────

class PingSession:
    """Track one burst of N pings dispatched from the source egg."""

    def __init__(self, n: int):
        self._n            = n
        self._acks         = 0
        self._rtts         = []
        self.t_first_ack   = None   # monotonic timestamp of first ACK confirmed
        self._started      = False
        self._done         = asyncio.Event()

    def on_line(self, line: str, now: float):
        if _START_RE.match(line) or _ROUTE_RE.match(line):
            self._started = True
        elif _DONE_RE.match(line):
            self._done.set()
        elif self._started:
            m = _ACK_RE.search(line)
            if m:
                self._acks += 1
                self._rtts.append(int(m.group(3)))
                if self.t_first_ack is None:
                    self.t_first_ack = now

    @property
    def pdr(self) -> float:
        return self._acks / self._n if self._n else 0.0

    @property
    def avg_rtt(self):
        return sum(self._rtts) / len(self._rtts) if self._rtts else None

    async def wait(self, timeout: float = 300.0) -> bool:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ── SourceEgg ─────────────────────────────────────────────────────────────────

class SourceEgg:
    """BLE connection to the source egg for sending pings and monitoring ACKs."""

    def __init__(self, client: BleakClient, node_id: int):
        self.client      = client
        self.node_id     = node_id
        self._buf        = ""
        self._session    = None
        self._loop       = asyncio.get_event_loop()
        self._rrep_event = asyncio.Event()
        self._rrep_info  = None   # (target, next_hop, hops) once RREP arrives

    async def _setup(self):
        await self.client.start_notify(NUS_TX_UUID, self._notify)
        await asyncio.sleep(0.5)

    async def send_ping(self, target: int, n: int) -> PingSession:
        s = PingSession(n)
        self._session = s
        await self.client.write_gatt_char(
            NUS_RX_UUID,
            bytes([CMD_PING, target & 0xFF, min(n, 255)]),
            response=True,
        )
        return s

    async def probe_route(self, target: int, timeout: float = 30.0) -> bool:
        """Send a single ping to target — triggers RREQ if no route exists.
        Waits for PING_ROUTE (route receipt) and returns True if confirmed."""
        self._rrep_event.clear()
        self._rrep_info = None
        await self.client.write_gatt_char(
            NUS_RX_UUID,
            bytes([CMD_PING, target & 0xFF, 1]),
            response=True,
        )
        try:
            await asyncio.wait_for(self._rrep_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _notify(self, _, data: bytearray):
        try:
            self._buf += data.decode("utf-8", errors="replace")
        except Exception:
            return
        now = _time.monotonic()
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            if _START_RE.match(line) or _DONE_RE.match(line):
                print(f"  [egg_{self.node_id}] {_GREEN}{line}{_RESET}")
                if _START_RE.match(line):
                    # PING_START always means route is confirmed — covers both the
                    # pre-cached case (no PING_ROUTE) and the RREQ→RREP path.
                    self._loop.call_soon_threadsafe(self._rrep_event.set)
            elif _WAIT_RE.match(line) or _ROUTE_RE.match(line):
                print(f"  [egg_{self.node_id}] {_YELLOW}{line}{_RESET}")
                if _ROUTE_RE.match(line):
                    parts = dict(p.split("=") for p in line.split()[1:] if "=" in p)
                    tgt = int(parts.get("target", 0))
                    nh  = int(parts.get("next_hop", 0))
                    self._rrep_info = (tgt, nh, None)
                    self._loop.call_soon_threadsafe(self._rrep_event.set)
            elif _SEND_RE.search(line):
                m = _SEND_RE.search(line)
                nh = m.group(4)
                stale = (hasattr(self, "killed_egg") and
                         self.killed_egg is not None and
                         nh == str(self.killed_egg))
                tag = f" {_RED}(stale — egg_{nh} killed){_RESET}" if stale else ""
                print(f"  [egg_{self.node_id}] {_CYAN}→ SEND seq={m.group(3)} "
                      f"dst=egg_{m.group(2)} via egg_{nh}{_RESET}{tag}")
            elif _ACK_RE.search(line):
                m = _ACK_RE.search(line)
                print(f"  [egg_{self.node_id}] {_GREEN}← ACK  seq={m.group(1)} "
                      f"from=egg_{m.group(2)} rtt_ms={m.group(3)}{_RESET}")
            if self._session:
                self._session.on_line(line, now)

    @classmethod
    @asynccontextmanager
    async def connect(cls, node_id: int):
        name = f"egg_{node_id}"
        print(f"Scanning for {name}...")
        dev = await BleakScanner.find_device_by_name(name, timeout=SCAN_TIMEOUT)
        if dev is None:
            raise RuntimeError(f"Cannot find {name} — is it powered and advertising?")
        print(f"  Found {name} ({dev.address})")
        async with BleakClient(dev) as client:
            egg = cls(client, node_id)
            await egg._setup()
            yield egg


# ── Route trace ──────────────────────────────────────────────────────────────

async def _show_route_trace(src_id: int, dst_id: int, node_ids: list):
    """Query route tables from all nodes and print the active forwarding path."""
    print(f"  Tracing active route egg_{src_id} → egg_{dst_id}...")
    try:
        async with TopologyCheck.connect(verbose=False) as tc:
            await tc.query_routes_all(node_ids=node_ids)
            await tc.collect_routes(timeout=10.0)
            path = tc.trace_path(src_id, dst_id)
            if path:
                hops = " → ".join(f"egg_{n}" for n in path)
                print(f"  {_CYAN}Active route: {_BOLD}{hops}{_RESET}")
            else:
                print(f"  {_YELLOW}Could not trace full path "
                      f"(partial route data — check serial logs){_RESET}")
    except Exception as e:
        print(f"  {_YELLOW}Route trace skipped: {e}{_RESET}")


# ── Phase 0: Mesh Formation ───────────────────────────────────────────────────

async def phase_formation(topo: Topology, timeout: float = 120.0):
    """Measure true mesh formation time with a software-synchronised start.

    Returns (formation_time_s, pass_bool).
    """
    expected_mesh = [n for n in topo.node_ids() if n != 99]

    print(f"\n{_BOLD}{'═'*62}{_RESET}")
    print(f"{_BOLD}  PHASE 0 — Mesh Formation{_RESET}")
    print(f"{_BOLD}{'═'*62}{_RESET}")
    print(f"  Nodes          : {sorted(expected_mesh)}")
    print(f"  {_YELLOW}Ensure all eggs are powered on, then press Enter.{_RESET}")
    await _ask("")

    async with TopologyCheck.connect() as tc:
        await tc.reset_all()
        print(f"  Reset sent — t=0. Waiting for nodes to self-report...\n")
        reports, _ = await tc.collect_formation(expected_mesh, timeout=timeout)

        missing = sorted(set(expected_mesh) - set(reports))
        if missing:
            print(f"\n  {_YELLOW}No report from : {missing}{_RESET}")

        print(f"\n  Verifying topology...")
        topo_ok = await tc.verify(topo.as_expected(), timeout=30.0)

    if not reports:
        print(f"  {_RED}No formation reports received{_RESET}")
        return 0.0, False

    ok = len(missing) == 0 and topo_ok
    ft = max(reports.values())
    col = _GREEN if ok else _YELLOW
    print(f"\n  Formation time : {col}{ft:.1f}s{_RESET}  "
          f"(max self-reported, from synchronized reset)")
    return ft, ok


# ── Phase 1: Setup & Formation ────────────────────────────────────────────────

async def phase_setup(topo: Topology, timeout: float):
    """Verify full topology via egg_99.  Returns (topology_check_time_s, pass_bool)."""
    print(f"\n{_BOLD}{'═'*62}{_RESET}")
    print(f"{_BOLD}  PHASE 1 — Topology Check{_RESET}")
    print(f"{_BOLD}{'═'*62}{_RESET}")
    print(f"  Expected nodes : {topo.node_ids()}")

    t0 = _time.monotonic()
    async with TopologyCheck.connect(timeout=timeout) as tc:
        ok = await tc.verify(topo.as_expected(), timeout=timeout)
    elapsed = _time.monotonic() - t0

    col = _GREEN if ok else _YELLOW
    print(f"  Check time     : {col}{elapsed:.1f}s{_RESET}")
    return elapsed, ok


# ── Route check + confirmation ────────────────────────────────────────────────

async def _confirm_route(src: "SourceEgg", dst: int):
    """Probe route from src to dst via RREQ.  Retries on failure.
    Asks user to confirm before PDR test starts."""
    while True:
        print(f"\n  {_BOLD}Route check  egg_{src.node_id} → egg_{dst}{_RESET}")
        print(f"  Sending RREQ to egg_{dst}...")
        ok = await src.probe_route(dst, timeout=30.0)
        if ok:
            info = src._rrep_info
            if info:
                print(f"  {_GREEN}Route confirmed: egg_{src.node_id} → "
                      f"egg_{info[1]} → … → egg_{dst}{_RESET}")
            ans = await _ask("  Start PDR test? [Enter to confirm / n to retry]: ")
            if ans.strip().lower() != "n":
                return
        else:
            print(f"  {_RED}No route reply from egg_{dst} within 30s{_RESET}")
            ans = await _ask("  Try again? [Enter to retry / n to skip]: ")
            if ans.strip().lower() == "n":
                return


# ── Phase 2: PDR Baseline ─────────────────────────────────────────────────────

async def phase_baseline(src: SourceEgg, dst: int, n: int, node_ids: list) -> float:
    """Send N pings A → C with no failures.  Returns PDR."""
    print(f"\n{_BOLD}{'═'*62}{_RESET}")
    print(f"{_BOLD}  PHASE 2 — PDR Baseline  "
          f"egg_{src.node_id} → egg_{dst}{_RESET}")
    print(f"{_BOLD}{'═'*62}{_RESET}")
    await _confirm_route(src, dst)
    await _show_route_trace(src.node_id, dst, node_ids)
    print(f"  Sending {n} pings...")

    s = await src.send_ping(dst, n)
    if not await s.wait(timeout=n * 20 + 30):
        print(f"  {_YELLOW}Timeout — using partial results{_RESET}")

    rtt = f"{s.avg_rtt:.0f}ms" if s.avg_rtt else "—"
    print(f"  PDR: {_col(s.pdr)}{s.pdr:.1%}{_RESET}  "
          f"({s._acks}/{n} ACKed)  avg RTT: {rtt}")
    return s.pdr


# ── Phase 3: One Rerouting Trial ──────────────────────────────────────────────

async def phase_trial(src: SourceEgg, dst: int,
                      n: int, trial: int, node_ids: list) -> dict:
    """Run one kill-B / reroute / restore-B trial.  Returns metrics dict."""
    via = 0   # set interactively at kill step
    print(f"\n{_BOLD}{'─'*62}{_RESET}")
    print(f"{_BOLD}  TRIAL {trial}  —  egg_{src.node_id} → egg_{dst}{_RESET}")
    print(f"{_BOLD}{'─'*62}{_RESET}")
    tout = n * 20 + 60   # generous timeout: rerouting can take 15-30 s per ping

    # ── Reset mesh state ──────────────────────────────────────────────────────
    expected_mesh = [nid for nid in node_ids if nid != 99]
    async with TopologyCheck.connect(verbose=False) as tc:
        await tc.reset_all()
        print(f"  Reset sent — waiting for mesh to reform...")
        reports, _ = await tc.collect_formation(expected_mesh, timeout=120.0)
    missing = sorted(set(expected_mesh) - set(reports))
    if missing:
        print(f"  {_YELLOW}No formation report from: {missing}{_RESET}")

    # ── Pre-kill baseline ──────────────────────────────────────────────────────
    await _confirm_route(src, dst)
    await _show_route_trace(src.node_id, dst, node_ids)
    print(f"  [pre-kill]   Sending {n} pings via nominal route...")
    s_pre = await src.send_ping(dst, n)
    if not await s_pre.wait(timeout=tout):
        print(f"  {_YELLOW}Pre-kill batch timed out — partial results{_RESET}")
    rtt_pre = f"{s_pre.avg_rtt:.0f}ms" if s_pre.avg_rtt else "—"
    print(f"  Pre-kill PDR: {_col(s_pre.pdr)}{s_pre.pdr:.1%}{_RESET}  "
          f"({s_pre._acks}/{n})  avg RTT: {rtt_pre}")

    # ── Kill B ────────────────────────────────────────────────────────────────
    raw = await _ask(f"\n  {_YELLOW}[ ACTION ]  Which egg to power off? "
                     f"Enter egg ID: {_RESET}")
    via = int(raw.strip()) if raw.strip().isdigit() else via
    print(f"  {_YELLOW}Powering off egg_{via}...{_RESET}")
    await _ask(f"  Press Enter once egg_{via} is off: ")
    t_kill = _time.monotonic()
    src.killed_egg = via

    # ── Transition batch (captures rerouting) ─────────────────────────────────
    print(f"\n  {_YELLOW}Note: cached route may still show egg_{via} until ACK "
          f"failures trigger RREQ — this is expected.{_RESET}")
    print(f"\n  [transition] Sending {n} pings — rerouting in progress...")
    s_tr = await src.send_ping(dst, n)
    if not await s_tr.wait(timeout=tout):
        print(f"  {_YELLOW}Transition batch timed out — partial results{_RESET}")

    reroute_s = None
    if s_tr.t_first_ack is not None and s_tr.t_first_ack >= t_kill:
        reroute_s = s_tr.t_first_ack - t_kill
    rt_str  = f"{reroute_s:.1f}s" if reroute_s is not None else "N/A (no ACK)"
    rtt_tr  = f"{s_tr.avg_rtt:.0f}ms" if s_tr.avg_rtt else "—"
    print(f"  Transition PDR: {_col(s_tr.pdr)}{s_tr.pdr:.1%}{_RESET}  "
          f"reroute time: {rt_str}  avg RTT: {rtt_tr}")

    # ── Post-reroute batch ────────────────────────────────────────────────────
    src.killed_egg = None
    print(f"\n  [post-route] Sending {n} pings — alternate route established...")
    s_post = await src.send_ping(dst, n)
    if not await s_post.wait(timeout=tout):
        print(f"  {_YELLOW}Post-reroute batch timed out — partial results{_RESET}")
    rtt_post = f"{s_post.avg_rtt:.0f}ms" if s_post.avg_rtt else "—"
    print(f"  Post-reroute PDR: {_col(s_post.pdr)}{s_post.pdr:.1%}{_RESET}  "
          f"({s_post._acks}/{n})  avg RTT: {rtt_post}")

    # ── Active route while B is still off ────────────────────────────────────
    await _show_route_trace(src.node_id, dst, node_ids)

    # ── Restore B ─────────────────────────────────────────────────────────────
    # Open the listener before prompting so we don't miss the report if egg_via
    # forms quickly after boot.
    rejoin_s = None
    async with TopologyCheck.connect(verbose=False) as tc:
        print(f"\n  {_YELLOW}[ ACTION ]  Power on egg_{via}.{_RESET}")
        await _ask(f"  Press Enter once egg_{via} is back on: ")
        print(f"  Waiting for egg_{via} to self-report rejoin (up to 180s)...")
        reports, _ = await tc.collect_formation([via], timeout=180.0)
    if via in reports:
        rejoin_s = reports[via]
        print(f"  {_GREEN}egg_{via} rejoined in {rejoin_s:.1f}s from boot{_RESET}")
    else:
        print(f"  {_RED}egg_{via} did not rejoin within 180s{_RESET}")

    return {
        "trial":        trial,
        "pdr_pre":      s_pre.pdr,
        "pdr_trans":    s_tr.pdr,
        "pdr_post":     s_post.pdr,
        "rtt_pre_ms":   s_pre.avg_rtt,
        "rtt_trans_ms": s_tr.avg_rtt,
        "rtt_post_ms":  s_post.avg_rtt,
        "reroute_s":    reroute_s,
        "auto_reroute": s_tr._acks > 0,
        "rejoin_s":     rejoin_s,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(ft: float, ft_ok: bool, base_pdr: float, trials: list):
    print(f"\n{_BOLD}{'═'*68}{_RESET}")
    print(f"{_BOLD}  TEST 1a — RESULTS SUMMARY{_RESET}")
    print(f"{_BOLD}{'═'*68}{_RESET}")

    def chk(b):
        return (_GREEN + "✓" + _RESET) if b else (_RED + "✗" + _RESET)

    print(f"\n  {_BOLD}Setup{_RESET}")
    print(f"  {chk(ft_ok)}  Topology verified     "
          f"formation_time={ft:.1f}s")
    print(f"  {chk(ft_ok)}  Topology vs allowlist  "
          f"{'PASS' if ft_ok else 'MISMATCH'}")

    print(f"\n  {_BOLD}Baseline{_RESET}")
    print(f"  {chk(base_pdr >= 0.9)}  PDR baseline          "
          f"{_col(base_pdr)}{base_pdr:.1%}{_RESET}")

    rts = [r["reroute_s"] for r in trials if r["reroute_s"] is not None]

    print(f"\n  {_BOLD}Rerouting Trials{_RESET}")
    hdr = (f"  {'T':>2}  {'PDR_pre':>8}  {'RTT_pre':>8}  {'PDR_trans':>9}  "
           f"{'RTT_tr':>7}  {'PDR_post':>9}  {'RTT_post':>9}  "
           f"{'Reroute_s':>9}  {'Auto':>5}  Rejoin_s")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for r in trials:
        rt      = f"{r['reroute_s']:>8.1f}" if r["reroute_s"]   is not None else f"{'N/A':>8}"
        rj      = f"{r['rejoin_s']:.1f}"    if r["rejoin_s"]    is not None else "N/A"
        ar      = (_GREEN + "yes" + _RESET) if r["auto_reroute"] else (_RED + "no" + _RESET)
        rtt_pre = f"{r['rtt_pre_ms']:.0f}ms"   if r["rtt_pre_ms"]   else "—"
        rtt_tr  = f"{r['rtt_trans_ms']:.0f}ms" if r["rtt_trans_ms"] else "—"
        rtt_pst = f"{r['rtt_post_ms']:.0f}ms"  if r["rtt_post_ms"]  else "—"
        print(
            f"  {r['trial']:>2}  "
            f"{_col(r['pdr_pre'])}{r['pdr_pre']:>7.1%}{_RESET}  "
            f"{rtt_pre:>8}  "
            f"{_col(r['pdr_trans'])}{r['pdr_trans']:>8.1%}{_RESET}  "
            f"{rtt_tr:>7}  "
            f"{_col(r['pdr_post'])}{r['pdr_post']:>8.1%}{_RESET}  "
            f"{rtt_pst:>9}  "
            f"{rt}  "
            f"{ar}  "
            f"{rj}"
        )

    if rts:
        print(f"\n  Avg reroute time : {sum(rts)/len(rts):.1f}s  "
              f"(min={min(rts):.1f}s  max={max(rts):.1f}s  n={len(rts)})")

    all_auto = all(r["auto_reroute"] for r in trials)
    all_post = all(r["pdr_post"] >= 0.8 for r in trials)
    overall  = ft_ok and all_auto and all_post
    label    = (_GREEN + "PASS" + _RESET) if overall else (_RED + "FAIL" + _RESET)
    print(f"\n  Overall: {_BOLD}{label}{_RESET}")
    print(f"{_BOLD}{'═'*68}{_RESET}\n")


def save_csv(ft: float, base_pdr: float, trials: list, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test", "1a",
                    "generated", datetime.datetime.now().isoformat()])
        w.writerow(["formation_time_s", f"{ft:.2f}"])
        w.writerow(["baseline_pdr",     f"{base_pdr:.3f}"])
        w.writerow([])
        w.writerow(["trial", "pdr_pre", "rtt_pre_ms", "pdr_transition",
                    "rtt_trans_ms", "pdr_post", "rtt_post_ms",
                    "reroute_time_s", "auto_reroute", "rejoin_time_s"])
        for r in trials:
            w.writerow([
                r["trial"],
                f"{r['pdr_pre']:.3f}",
                f"{r['rtt_pre_ms']:.0f}"   if r["rtt_pre_ms"]   else "",
                f"{r['pdr_trans']:.3f}",
                f"{r['rtt_trans_ms']:.0f}" if r["rtt_trans_ms"] else "",
                f"{r['pdr_post']:.3f}",
                f"{r['rtt_post_ms']:.0f}"  if r["rtt_post_ms"]  else "",
                f"{r['reroute_s']:.2f}"    if r["reroute_s"]    is not None else "",
                "yes" if r["auto_reroute"] else "no",
                f"{r['rejoin_s']:.2f}"     if r["rejoin_s"]     is not None else "",
            ])
    print(f"  Results saved to {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run(args):
    try:
        topo = Topology.load(args.topology)
    except Exception as e:
        sys.exit(f"Cannot load topology '{args.topology}': {e}")

    for issue in topo.validate():
        print(f"{_YELLOW}Topology: {issue}{_RESET}")

    paths = all_paths(topo, args.source, args.dest)
    print(f"\n{_BOLD}Paths  egg_{args.source} → egg_{args.dest}:{_RESET}")
    for p in paths:
        print(f"  {'→'.join(f'egg_{n}' for n in p)}")
    if len(paths) < 2:
        print(f"{_RED}Warning: fewer than 2 distinct paths declared — "
              f"multi-path test cannot be verified from topology file.{_RESET}")
    ft_form, ft_ok = await phase_formation(topo, timeout=args.formation_timeout)

    node_ids = list(topo.node_ids())
    async with SourceEgg.connect(args.source) as src:
        base_pdr = await phase_baseline(src, args.dest, args.pings, node_ids)

        trials = []
        for i in range(1, args.trials + 1):
            r = await phase_trial(src, args.dest, args.pings, i, node_ids)
            trials.append(r)
            if i < args.trials:
                await _ask(f"\n  Ready for trial {i + 1} — press Enter: ")

    print_report(ft_form, ft_ok, base_pdr, trials)

    stamp    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join("test_results", f"test1a_{stamp}.csv")
    save_csv(ft_form, base_pdr, trials, csv_path)


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Test 1a — Well-Connected Mesh: "
            "formation check + multi-path rerouting"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_1a_mesh_check.py topology.json --source 1 --dest 3
  python test_1a_mesh_check.py topology.json --source 1 --dest 3 --trials 5 --pings 20
        """,
    )
    ap.add_argument("topology", help="Topology JSON file")
    ap.add_argument("--source", type=int, required=True, metavar="ID",
                    help="Source node (A)")
    ap.add_argument("--dest",   type=int, required=True, metavar="ID",
                    help="Destination node (C)")
    ap.add_argument("--trials", type=int, default=5,
                    help="Rerouting trials (default 5)")
    ap.add_argument("--pings",  type=int, default=10,
                    help="Pings per measurement batch (default 20)")
    ap.add_argument("--formation-timeout", type=float, default=120.0,
                    help="Seconds to wait for formation beacons and topology check (default 120)")
    args = ap.parse_args()

    if args.source == args.dest:
        ap.error("--source and --dest must be different nodes")

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

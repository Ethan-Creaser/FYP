"""
logger.py — Scenario Logger
=============================
Logs events to CSV for scenario validation and FYP report.

Events logged:
  - Node discovery (time from boot)
  - Node localisation (time + position)
  - Node offline / recovery
  - Position updates (from DIST messages)
  - Sub-network events

Usage:
    from logger import Logger
    log = Logger("scenario1")
    log.node_localised(1, x, y, z)
    log.node_offline(2)
    log.node_recovered(2, x, y, z, recovery_time_s)
"""

import csv
import time
import os


class Logger:

    def __init__(self, scenario_name="run"):
        self._start_time = time.time()
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = "{}_{}.csv".format(scenario_name, ts)
        os.makedirs("logs", exist_ok=True)
        self._path = os.path.join("logs", filename)
        self._file = open(self._path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow([
            "elapsed_s", "event", "node_id",
            "x", "y", "z",
            "detail"
        ])
        self._file.flush()
        self._node_localised_at = {}  # id → elapsed_s when localised
        self._node_offline_at   = {}  # id → elapsed_s when went offline
        print("[Logger] Logging to", self._path)

    def _elapsed(self):
        return round(time.time() - self._start_time, 3)

    def _write(self, event, node_id=None, x=None, y=None, z=None, detail=""):
        self._writer.writerow([
            self._elapsed(), event, node_id,
            round(x, 4) if x is not None else "",
            round(y, 4) if y is not None else "",
            round(z, 4) if z is not None else "",
            detail
        ])
        self._file.flush()

    # ── Events ────────────────────────────────────────────────────────────────

    def node_discovered(self, node_id):
        """Node first heard via LoRa PONG."""
        elapsed = self._elapsed()
        print("[Logger] Node {} discovered at {:.1f}s".format(node_id, elapsed))
        self._write("DISCOVERED", node_id, detail="elapsed={:.1f}s".format(elapsed))

    def node_localised(self, node_id, x, y, z):
        """Node received its first valid position."""
        elapsed = self._elapsed()
        self._node_localised_at[node_id] = elapsed
        print("[Logger] Node {} localised at {:.1f}s → ({:.3f},{:.3f},{:.3f})".format(
            node_id, elapsed, x, y, z))
        self._write("LOCALISED", node_id, x, y, z,
                    "elapsed={:.1f}s".format(elapsed))

    def node_position_updated(self, node_id, x, y, z, prev_x=None, prev_y=None, prev_z=None):
        """Position updated from DIST message (laptop trilateration)."""
        detail = ""
        if prev_x is not None:
            import math
            delta = math.sqrt((x-prev_x)**2+(y-prev_y)**2+(z-prev_z)**2)
            detail = "delta={:.3f}m".format(delta)
        self._write("POSITION_UPDATE", node_id, x, y, z, detail)

    def node_offline(self, node_id):
        """Node stopped responding to heartbeats."""
        elapsed = self._elapsed()
        self._node_offline_at[node_id] = elapsed
        print("[Logger] Node {} OFFLINE at {:.1f}s".format(node_id, elapsed))
        self._write("OFFLINE", node_id,
                    detail="elapsed={:.1f}s".format(elapsed))

    def node_recovered(self, node_id, x, y, z):
        """Node came back online and re-localised."""
        elapsed = self._elapsed()
        offline_at = self._node_offline_at.get(node_id)
        recovery_time = round(elapsed - offline_at, 1) if offline_at else "?"
        print("[Logger] Node {} RECOVERED at {:.1f}s (recovery={})".format(
            node_id, elapsed, recovery_time))
        self._write("RECOVERED", node_id, x, y, z,
                    "recovery_time={}s".format(recovery_time))

    def network_split(self, group_a, group_b):
        """Two isolated sub-networks detected."""
        detail = "groupA={} groupB={}".format(group_a, group_b)
        print("[Logger] Network split detected:", detail)
        self._write("NETWORK_SPLIT", detail=detail)

    def network_merged(self, all_nodes):
        """Sub-networks unified into one."""
        detail = "nodes={}".format(all_nodes)
        print("[Logger] Network merged:", detail)
        self._write("NETWORK_MERGED", detail=detail)

    def custom(self, event, node_id=None, detail=""):
        """Log a custom event."""
        self._write(event, node_id, detail=detail)

    def close(self):
        self._file.close()
        print("[Logger] Closed:", self._path)

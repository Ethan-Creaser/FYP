"""
gateway.py — Node 0 Gateway with Round-Robin Ranging
======================================================
Phases:
  1. DISCOVERY  — find all nodes via LoRa PING/PONG
  2. RANGING    — coordinate round-robin: each node takes a turn as tag,
                  measures all others, broadcasts DIST_REPORT
  3. SOLVE      — collect all DIST_REPORTs, build full distance matrix,
                  run MDS to get globally consistent positions
  4. STEADY     — broadcast positions, heartbeat, handle late/offline nodes

The laptop connects passively and receives MAP messages.
"""

import utime, gc
from machine import reset as hard_reset
from localise import solve_from_distance_matrix

# ── Timing ────────────────────────────────────────────────────────────────────
PING_INTERVAL_MS      = 2_000
PING_COLLECT_MS       = 3_000
PING_TIMEOUT_MS       = 180_000
RANGE_TURN_WAIT_MS    = 40_000   # max wait per node during ranging
RANGE_TURN_GAP_MS     = 2_000    # gap between turns
MAP_REBROADCAST_MS    = 15_000
HEARTBEAT_INTERVAL_MS = 30_000
HEARTBEAT_TIMEOUT_MS  = 90_000
OLED_REFRESH_MS       = 1_000
UWB_SCAN_FRAMES       = 20


def emit(obj):
    import ujson
    print(ujson.dumps(obj))


class GatewayNode:

    def __init__(self, comms, uwb, oled=None):
        self.comms        = comms
        self.uwb          = uwb
        self.oled         = oled
        self.known_nodes  = set()   # all discovered node IDs
        self.node_map     = {}      # id → {x,y,z,rssi,snr,last_seen}
        self.dist_matrix  = {}      # (a,b) → distance_m
        self._last_seen   = {}
        self._last_self   = 0
        self._last_hb     = 0
        self._last_oled   = 0
        self._phase       = "BOOT"

    # ── Entry ─────────────────────────────────────────────────────────────────

    def run(self):
        print("[Node 0] Gateway started")
        emit({"type":"MAP","id":0,"x":0.0,"y":0.0,"z":0.0,"rssi":0,"snr":0.0})
        self._oled("GATEWAY\nBOOT")

        self._phase_discovery()
        self._phase_ranging()
        self._phase_solve()
        self._phase_steady()

    # ── Phase 1: DISCOVERY ────────────────────────────────────────────────────

    def _phase_discovery(self):
        """Find all nodes via LoRa PING/PONG before starting ranging."""
        self._phase = "DISCOVERY"
        self._oled("DISCOVERY\nPinging...")

        # Switch UWB to anchor — we don't need it in discovery
        self.uwb.configure(0, role=1)

        last_ping  = utime.ticks_add(utime.ticks_ms(), -PING_INTERVAL_MS)
        ping_start = utime.ticks_ms()

        print("[Node 0] Discovering nodes...")
        while True:
            now = utime.ticks_ms()
            if utime.ticks_diff(now, last_ping) >= PING_INTERVAL_MS:
                self.comms.send({"type":"PING","id":0})
                last_ping = utime.ticks_ms()
                print("[Node 0] PING — known: {}".format(self.known_nodes))

            msg = self.comms.recv()
            if msg and msg.get("type") == "PONG":
                nid = msg.get("id")
                if nid and nid != 0:
                    if nid not in self.known_nodes:
                        self.known_nodes.add(nid)
                        print("[Node 0] Discovered node {}".format(nid))
                    self._last_seen[nid] = utime.ticks_ms()

            if utime.ticks_diff(now, ping_start) > PING_TIMEOUT_MS:
                print("[Node 0] Discovery timeout — hard reset")
                hard_reset()

            # Stop after collecting nodes for at least PING_COLLECT_MS
            # since the first node was discovered
            if (self.known_nodes and
                    utime.ticks_diff(now, ping_start) >= PING_COLLECT_MS):
                # One final collection window
                drain = utime.ticks_add(utime.ticks_ms(), PING_COLLECT_MS)
                while utime.ticks_diff(drain, utime.ticks_ms()) > 0:
                    msg = self.comms.recv()
                    if msg and msg.get("type") == "PONG":
                        nid = msg.get("id")
                        if nid and nid != 0:
                            self.known_nodes.add(nid)
                    utime.sleep_ms(50)
                break

            utime.sleep_ms(50)

        print("[Node 0] Discovery done. Nodes: {}".format(self.known_nodes))
        self._oled("DISCOVERY\n{} nodes".format(len(self.known_nodes)))

    # ── Phase 2: RANGING ──────────────────────────────────────────────────────

    def _phase_ranging(self):
        """
        Round-robin ranging: each node takes a turn as tag and measures
        all others. Node 0 also participates as tag.
        Collects DIST_REPORT messages to build the full distance matrix.
        """
        self._phase = "RANGING"
        all_nodes = sorted([0] + list(self.known_nodes))
        print("[Node 0] Starting round-robin ranging: {}".format(all_nodes))

        for turn_node in all_nodes:
            self._oled("RANGING\nNode {} turn".format(turn_node))
            print("[Node 0] Range turn: node {}".format(turn_node))

            if turn_node == 0:
                # Node 0 does its own ranging
                self._do_own_ranging(all_nodes)
            else:
                # Tell another node to range
                self.comms.send({"type":"RANGE_TURN","id":turn_node,
                                 "nodes":all_nodes})
                # Wait for its DIST_REPORT
                deadline = utime.ticks_add(utime.ticks_ms(), RANGE_TURN_WAIT_MS)
                got_report = False
                while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
                    msg = self.comms.recv()
                    if msg:
                        t = msg.get("type"); nid = msg.get("id")
                        if t == "DIST_REPORT" and nid == turn_node:
                            self._record_dist_report(msg)
                            got_report = True
                            break
                        elif t == "PONG" and nid and nid != 0:
                            self._last_seen[nid] = utime.ticks_ms()
                    utime.sleep_ms(50)

                if not got_report:
                    print("[Node 0] No report from node {} — skipping".format(turn_node))

            utime.sleep_ms(RANGE_TURN_GAP_MS)
            gc.collect()

        print("[Node 0] Ranging complete. Matrix: {}".format(len(self.dist_matrix)))

    def _do_own_ranging(self, all_nodes):
        """Node 0 switches to tag, ranges all others, records distances."""
        print("[Node 0] Own ranging turn...")
        self._oled("RANGING\nNode 0 tag")

        try:
            self.uwb.configure(0, role=0)
            utime.sleep_ms(2000)
            self.uwb.flush()
            raw = self.uwb.scan(UWB_SCAN_FRAMES)
            dists = sorted([d for d in raw.values() if d and d > 0])
            print("[Node 0] Own distances: {}".format(dists))
        except Exception as e:
            print("[Node 0] Own ranging error: {}".format(e))
            dists = []
        finally:
            self.uwb.configure(0, role=1)

        # Match distances to other nodes (sorted by ID)
        others = [n for n in all_nodes if n != 0]
        for i, nid in enumerate(others):
            if i < len(dists):
                self.dist_matrix[(0, nid)] = dists[i]
                print("[Node 0]   d(0,{}) = {:.3f}m".format(nid, dists[i]))

    def _record_dist_report(self, msg):
        """Store distances from a DIST_REPORT message into the matrix."""
        src  = msg.get("id")
        dists = msg.get("dists", {})  # dict of str(target_id) → distance
        rssi = self.comms.rssi()
        self._last_seen[src] = utime.ticks_ms()
        for target_str, d in dists.items():
            try:
                target = int(target_str)
                if target != src and d and d > 0:
                    self.dist_matrix[(src, target)] = float(d)
                    print("[Node 0]   d({},{}) = {:.3f}m".format(src, target, d))
            except: pass

    # ── Phase 3: SOLVE ────────────────────────────────────────────────────────

    def _phase_solve(self):
        """Run MDS on the full distance matrix to get global positions."""
        self._phase = "SOLVE"
        self._oled("SOLVING\nMDS...")

        all_nodes = sorted([0] + list(self.known_nodes))
        print("[Node 0] Running MDS on {} nodes...".format(len(all_nodes)))

        try:
            positions = solve_from_distance_matrix(all_nodes, self.dist_matrix)
        except Exception as e:
            print("[Node 0] MDS failed: {}".format(e))
            positions = {0: (0.0, 0.0, 0.0)}

        now = utime.ticks_ms()
        for nid, (x, y, z) in positions.items():
            self.node_map[nid] = {
                "x": round(x,4), "y": round(y,4), "z": round(z,4),
                "rssi": 0, "snr": 0.0, "last_seen": now
            }
            self._last_seen[nid] = now
            emit({"type":"MAP","id":nid,
                  "x":round(x,4),"y":round(y,4),"z":round(z,4),
                  "rssi":0,"snr":0.0})
            print("[Node 0] Node {} → ({:.3f},{:.3f},{:.3f})".format(nid,x,y,z))

        # Broadcast all positions to nodes so they know where they are
        for nid, entry in self.node_map.items():
            self.comms.send({"type":"MAP","id":nid,
                             "x":entry["x"],"y":entry["y"],"z":entry["z"]})
            utime.sleep_ms(100)

        print("[Node 0] Solve complete")
        self._oled("SOLVED\n{} nodes".format(len(self.node_map)))

        # Announce ourselves as coordinator so followers know
        self.comms.send({"type":"COORD","id":0,"coord_id":0})
        emit({"type":"COORD","id":0,"coord_id":0})

    # ── Phase 4: STEADY ───────────────────────────────────────────────────────

    def _phase_steady(self):
        """
        Maintain network, handle new nodes, periodic re-ranging.
        Laptop connects passively and receives MAP messages.
        """
        self._phase = "STEADY"
        self.comms.send({"type":"PING","id":0})

        while True:
            now = utime.ticks_ms()
            msg = self.comms.recv()

            if msg:
                t = msg.get("type"); nid = msg.get("id")

                if nid and nid != 0:
                    if nid not in self.known_nodes:
                        # New node joined — add to network and trigger re-ranging
                        self.known_nodes.add(nid)
                        print("[Node 0] New node {} joined — queuing re-range".format(nid))
                    self._last_seen[nid] = utime.ticks_ms()

                if t == "MAP" and nid is not None:
                    self._update_map(msg)

                elif t == "DIST_REPORT" and nid and nid != 0:
                    # A node completed a position check — update matrix
                    self._record_dist_report(msg)
                    # Re-solve with updated distances
                    self._resolve()

                elif t == "REQUEST_MAP":
                    # Rover came into range — send everything
                    print("[Node 0] REQUEST_MAP from {} — broadcasting all".format(nid))
                    self._broadcast_all_positions()

                elif t == "COORD" and nid and nid != 0:
                    # Another node declared coordinator while we were offline
                    # Re-assert ourselves since we're Node 0 (lowest ID)
                    print("[Node 0] Heard COORD from {} — re-asserting".format(nid))
                    utime.sleep_ms(1000)
                    self.comms.send({"type":"COORD","id":0,"coord_id":0})
                    self._broadcast_all_positions()

                elif t == "NEED_TAG" and nid and nid != 0:
                    # Unlocalized node — run a mini ranging session for it
                    print("[Node 0] Node {} needs ranging".format(nid))
                    self._mini_range(nid)

            if utime.ticks_diff(now, self._last_self) >= MAP_REBROADCAST_MS:
                self._broadcast_all_positions()
                self._last_self = utime.ticks_ms()

            if utime.ticks_diff(now, self._last_hb) >= HEARTBEAT_INTERVAL_MS:
                self._heartbeat()
                self._last_hb = utime.ticks_ms()

            self._periodic_oled()
            gc.collect()
            utime.sleep_ms(50)

    def _resolve(self):
        """Re-run MDS when new distance data arrives."""
        all_nodes = sorted(self.node_map.keys())
        if len(all_nodes) < 2:
            return
        try:
            positions = solve_from_distance_matrix(all_nodes, self.dist_matrix)
            now = utime.ticks_ms()
            for nid, (x, y, z) in positions.items():
                if nid in self.node_map:
                    self.node_map[nid].update({"x":round(x,4),"y":round(y,4),
                                               "z":round(z,4),"last_seen":now})
                    emit({"type":"MAP","id":nid,"x":round(x,4),
                          "y":round(y,4),"z":round(z,4),
                          "rssi":self.node_map[nid].get("rssi",0),
                          "snr":self.node_map[nid].get("snr",0.0)})
        except Exception as e:
            print("[Node 0] Re-solve error: {}".format(e))

    def _mini_range(self, new_nid):
        """Quick ranging session to add a new node to the network."""
        all_nodes = sorted([0] + list(self.known_nodes))
        self.comms.send({"type":"RANGE_TURN","id":new_nid,"nodes":all_nodes})
        deadline = utime.ticks_add(utime.ticks_ms(), RANGE_TURN_WAIT_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            msg = self.comms.recv()
            if msg and msg.get("type") == "DIST_REPORT" and msg.get("id") == new_nid:
                self._record_dist_report(msg)
                self._resolve()
                break
            utime.sleep_ms(50)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_map(self, msg):
        nid = msg.get("id")
        if nid is None: return
        rssi = self.comms.rssi()
        snr  = round(self.comms.snr(), 2)
        entry = {
            "x": float(msg.get("x",0)), "y": float(msg.get("y",0)),
            "z": float(msg.get("z",0)), "rssi": rssi, "snr": snr,
            "last_seen": utime.ticks_ms()
        }
        self.node_map[nid] = entry
        emit({"type":"MAP","id":nid,
              "x":round(entry["x"],4),"y":round(entry["y"],4),
              "z":round(entry["z"],4),"rssi":rssi,"snr":snr})

    def _broadcast_all_positions(self):
        self._last_self = utime.ticks_ms()
        self.comms.send({"type":"MAP","id":0,"x":0.0,"y":0.0,"z":0.0})
        emit({"type":"MAP","id":0,"x":0.0,"y":0.0,"z":0.0,"rssi":0,"snr":0.0})
        for nid, e in self.node_map.items():
            if nid != 0:
                self.comms.send({"type":"MAP","id":nid,
                    "x":e["x"],"y":e["y"],"z":e["z"]})

    def _heartbeat(self):
        self.comms.send({"type":"HEARTBEAT","id":0})
        now = utime.ticks_ms()
        for nid in list(self.node_map.keys()):
            if nid == 0: continue
            last = self._last_seen.get(nid)
            if last and utime.ticks_diff(now, last) > HEARTBEAT_TIMEOUT_MS:
                print("[Node 0] Node {} offline".format(nid))
                del self.node_map[nid]
                self._last_seen.pop(nid, None)
                emit({"type":"OFFLINE","id":nid})

    def _periodic_oled(self):
        now = utime.ticks_ms()
        if utime.ticks_diff(now, self._last_oled) >= OLED_REFRESH_MS:
            self._oled("{}\nNodes:{}".format(self._phase, len(self.node_map)))
            self._last_oled = utime.ticks_ms()

    def _oled(self, text):
        if not self.oled: return
        try: self.oled.display_text(text)
        except: pass

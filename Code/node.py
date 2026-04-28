"""
node.py — Follower Node with Coordinator Failover
===================================================
Every follower node monitors the coordinator (Node 0 or elected leader).
If the coordinator goes silent, the lowest-ID active node steps up.

States:
  WAITING     → no position yet, sending NEED_TAG
  ANCHORED    → localised, acting as UWB anchor
  COORDINATING → this node has taken over as network coordinator

Coordinator responsibilities (when stepping up):
  - Send HEARTBEAT
  - Broadcast all known positions
  - Handle NEED_TAG from new nodes
  - Run mini ranging sessions for new nodes
"""

import utime, math, gc
from localise import solve_position, solve_from_distance_matrix

# ── Timing ────────────────────────────────────────────────────────────────────
PONG_INTERVAL_MS     = 10_000
MAP_BROADCAST_MS     = 15_000
RANGE_CHECK_MS       = 60_000
NEED_TAG_MS          = 15_000
MOVE_THRESHOLD_M     = 0.40
MOVE_CONFIRM_SCANS   = 3
ANCHOR_SETTLE_MS     = 3_000
UWB_SCAN_FRAMES      = 20
UWB_QUICK_FRAMES     = 5
OLED_REFRESH_MS      = 1_000
COORD_TIMEOUT_MS     = 90_000   # declare self coordinator after this silence
COORD_ELECT_DELAY_MS = 3_000    # wait before declaring (let lower IDs go first)
HEARTBEAT_INTERVAL_MS = 30_000
HEARTBEAT_TIMEOUT_MS  = 90_000
RANGE_TURN_WAIT_MS    = 40_000


def emit(obj):
    import ujson
    print(ujson.dumps(obj))


class FollowerNode:

    def __init__(self, node_id, comms, uwb, oled=None):
        self.node_id         = node_id
        self.comms           = comms
        self.uwb             = uwb
        self.oled            = oled
        self.coords          = None
        self.known_anchors   = {}     # id → (x,y,z)
        self.node_map        = {}     # id → {x,y,z} — full network map
        self.dist_matrix     = {}     # (a,b) → distance
        self.known_nodes     = set()  # all known node IDs
        self._coord_id       = 0      # current coordinator ID
        self._last_coord_msg = utime.ticks_ms()  # last time coord was heard
        self._is_coord       = False
        self._last_pong      = 0
        self._last_map       = 0
        self._last_check     = 0
        self._last_need      = 0
        self._last_oled      = 0
        self._last_hb        = 0
        self._last_seen      = {}
        self._boot_time      = utime.ticks_ms()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        print("[Node {}] Starting".format(self.node_id))
        self._oled("ID:{}\nBooting...".format(self.node_id))
        self.uwb.configure(self.node_id, role=1)
        self._oled("ID:{}\nWaiting...".format(self.node_id))

        while True:
            now = utime.ticks_ms()

            msg = self.comms.recv()
            if msg:
                self._handle(msg)

            # ── Coordinator watchdog ───────────────────────────────────────
            if not self._is_coord:
                coord_age = utime.ticks_diff(now, self._last_coord_msg)
                if coord_age > COORD_TIMEOUT_MS and self.coords:
                    self._consider_takeover(now)

            # ── Regular duties ─────────────────────────────────────────────
            if utime.ticks_diff(now, self._last_pong) >= PONG_INTERVAL_MS:
                self._send_pong()
                self._last_pong = utime.ticks_ms()

            if (not self.coords and
                    utime.ticks_diff(now, self._boot_time) >= 10_000 and
                    utime.ticks_diff(now, self._last_need) >= NEED_TAG_MS):
                self.comms.send({"type":"NEED_TAG","id":self.node_id})
                self._last_need = utime.ticks_ms()

            if self.coords and utime.ticks_diff(now, self._last_map) >= MAP_BROADCAST_MS:
                self._broadcast_map()
                self._last_map = utime.ticks_ms()

            if (self.coords and
                    utime.ticks_diff(now, self._last_check) >= RANGE_CHECK_MS):
                self._check_position()
                self._last_check = utime.ticks_ms()

            # ── Coordinator duties (if elected) ────────────────────────────
            if self._is_coord:
                if utime.ticks_diff(now, self._last_hb) >= HEARTBEAT_INTERVAL_MS:
                    self._do_heartbeat()
                    self._last_hb = utime.ticks_ms()

                if utime.ticks_diff(now, self._last_map) >= MAP_BROADCAST_MS:
                    self._broadcast_all_positions()

            if utime.ticks_diff(now, self._last_oled) >= OLED_REFRESH_MS:
                self._refresh_oled()
                self._last_oled = utime.ticks_ms()

            gc.collect()
            utime.sleep_ms(50)

    # ── Message handling ──────────────────────────────────────────────────────

    def _handle(self, msg):
        t   = msg.get("type")
        nid = msg.get("id")

        # Track coordinator liveness
        if t in ("HEARTBEAT", "COORD", "MAP", "RANGE_TURN") and nid == self._coord_id:
            self._last_coord_msg = utime.ticks_ms()

        # Track all known nodes
        if nid is not None and nid != self.node_id:
            self.known_nodes.add(nid)
            self._last_seen[nid] = utime.ticks_ms()

        if t == "PING":
            self._send_pong()

        elif t == "HEARTBEAT":
            self._send_pong() if not self.coords else self._broadcast_map()

        elif t == "REQUEST_MAP":
            if self.coords:
                self._broadcast_map()
            if self._is_coord:
                self._broadcast_all_positions()

        elif t == "COORD":
            # Another node declared itself coordinator
            new_coord = msg.get("coord_id", nid)
            if new_coord != self._coord_id:
                print("[Node {}] New coordinator: {}".format(self.node_id, new_coord))
                self._coord_id = new_coord
                self._last_coord_msg = utime.ticks_ms()
            # If we were coordinator but a lower ID stepped up, step down
            if self._is_coord and new_coord < self.node_id:
                print("[Node {}] Stepping down — node {} is lower".format(
                    self.node_id, new_coord))
                self._is_coord = False

        elif t in ("MAP", "PONG"):
            if nid is not None and nid != self.node_id:
                x = float(msg.get("x", 0))
                y = float(msg.get("y", 0))
                z = float(msg.get("z", 0))
                if t == "MAP" or (x != 0 or y != 0 or z != 0):
                    self.known_anchors[nid] = (x, y, z)
                    self.node_map[nid] = {"x":x,"y":y,"z":z}
                    if self._is_coord:
                        emit({"type":"MAP","id":nid,
                              "x":round(x,4),"y":round(y,4),"z":round(z,4),
                              "rssi":self.comms.rssi(),"snr":round(self.comms.snr(),2)})

        elif t == "RANGE_TURN" and nid == self.node_id:
            nodes = msg.get("nodes", [])
            self._do_range_turn(nodes)

        elif t == "DIST_REPORT" and nid and nid != self.node_id:
            self._record_dist_report(msg)
            if self._is_coord:
                self._resolve_and_emit()

        elif t == "NEED_TAG" and nid and nid != self.node_id:
            if self._is_coord:
                print("[Node {}] (coord) Node {} needs ranging".format(
                    self.node_id, nid))
                self._mini_range(nid)

    # ── Coordinator takeover ──────────────────────────────────────────────────

    def _consider_takeover(self, now):
        """
        Coordinator has been silent for COORD_TIMEOUT_MS.
        Wait node_id * COORD_ELECT_DELAY_MS so lower IDs get priority.
        """
        # Check if anyone else already declared
        active_lower = [n for n in self.known_nodes
                        if n < self.node_id and
                        utime.ticks_diff(now, self._last_seen.get(n, 0)) < COORD_TIMEOUT_MS]
        if active_lower:
            # A lower-ID node is still active — let them take over
            return

        # Wait proportional to our ID to avoid simultaneous elections
        wait = self.node_id * COORD_ELECT_DELAY_MS
        utime.sleep_ms(min(wait, 10_000))

        # Check one more time that no one else declared
        msg = self.comms.recv()
        if msg and msg.get("type") == "COORD":
            self._handle(msg)
            return

        # Declare ourselves coordinator
        print("[Node {}] Coordinator {} gone — taking over".format(
            self.node_id, self._coord_id))
        self._coord_id  = self.node_id
        self._is_coord  = True
        self._last_coord_msg = utime.ticks_ms()
        self._last_hb   = utime.ticks_ms()

        self.comms.send({"type":"COORD","id":self.node_id,
                         "coord_id":self.node_id})
        emit({"type":"COORD","id":self.node_id,"coord_id":self.node_id})

        # Immediately broadcast all known positions
        self._broadcast_all_positions()
        print("[Node {}] Now coordinating {} nodes".format(
            self.node_id, len(self.node_map)))
        self._oled("COORD\nID:{}".format(self.node_id))

    # ── Coordinator duties ────────────────────────────────────────────────────

    def _do_heartbeat(self):
        self.comms.send({"type":"HEARTBEAT","id":self.node_id})
        now = utime.ticks_ms()
        for nid in list(self.node_map.keys()):
            if nid == self.node_id: continue
            last = self._last_seen.get(nid)
            if last and utime.ticks_diff(now, last) > HEARTBEAT_TIMEOUT_MS:
                print("[Node {}] (coord) Node {} offline".format(self.node_id, nid))
                del self.node_map[nid]
                self.known_anchors.pop(nid, None)
                self._last_seen.pop(nid, None)
                emit({"type":"OFFLINE","id":nid})

    def _broadcast_all_positions(self):
        self._last_map = utime.ticks_ms()
        # Emit self
        if self.coords:
            x,y,z = self.coords
            self.comms.send({"type":"MAP","id":self.node_id,
                             "x":round(x,4),"y":round(y,4),"z":round(z,4)})
            emit({"type":"MAP","id":self.node_id,
                  "x":round(x,4),"y":round(y,4),"z":round(z,4),"rssi":0,"snr":0.0})
        # Emit all known nodes
        for nid, e in self.node_map.items():
            if nid == self.node_id: continue
            self.comms.send({"type":"MAP","id":nid,
                             "x":e["x"],"y":e["y"],"z":e["z"]})
            emit({"type":"MAP","id":nid,
                  "x":e["x"],"y":e["y"],"z":e["z"],"rssi":0,"snr":0.0})

    def _mini_range(self, new_nid):
        """Tell a new node to range and solve its position."""
        all_nodes = sorted(self.node_map.keys())
        self.comms.send({"type":"RANGE_TURN","id":new_nid,"nodes":all_nodes})
        deadline = utime.ticks_add(utime.ticks_ms(), RANGE_TURN_WAIT_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            msg = self.comms.recv()
            if msg:
                if msg.get("type") == "DIST_REPORT" and msg.get("id") == new_nid:
                    self._record_dist_report(msg)
                    self._resolve_and_emit()
                    break
                else:
                    self._handle(msg)
            utime.sleep_ms(50)

    def _record_dist_report(self, msg):
        src   = msg.get("id")
        dists = msg.get("dists", {})
        self._last_seen[src] = utime.ticks_ms()
        for target_str, d in dists.items():
            try:
                target = int(target_str)
                if target != src and d and d > 0:
                    self.dist_matrix[(src, target)] = float(d)
            except: pass

    def _resolve_and_emit(self):
        all_nodes = sorted(self.node_map.keys())
        if len(all_nodes) < 2: return
        try:
            positions = solve_from_distance_matrix(all_nodes, self.dist_matrix)
            now = utime.ticks_ms()
            for nid, (x, y, z) in positions.items():
                self.node_map[nid] = {"x":round(x,4),"y":round(y,4),"z":round(z,4)}
                if nid in self.known_anchors:
                    self.known_anchors[nid] = (x, y, z)
                emit({"type":"MAP","id":nid,"x":round(x,4),
                      "y":round(y,4),"z":round(z,4),"rssi":0,"snr":0.0})
        except Exception as e:
            print("[Node {}] Resolve error: {}".format(self.node_id, e))

    # ── Range turn ────────────────────────────────────────────────────────────

    def _do_range_turn(self, all_nodes):
        self._oled("ID:{}\nRanging...".format(self.node_id))
        dists = {}
        try:
            if self.coords:
                self.uwb.configure_warm(self.node_id, role=0)
            else:
                self.uwb.configure(self.node_id, role=0)

            self.comms.send({"type":"REQUEST_MAP","id":self.node_id})
            end = utime.ticks_add(utime.ticks_ms(), ANCHOR_SETTLE_MS)
            last_req = utime.ticks_ms()
            while utime.ticks_diff(end, utime.ticks_ms()) > 0:
                msg = self.comms.recv()
                if msg and msg.get("type") in ("MAP","PONG"):
                    src = msg.get("id")
                    if src and src != self.node_id:
                        x=float(msg.get("x",0)); y=float(msg.get("y",0)); z=float(msg.get("z",0))
                        if msg.get("type")=="MAP" or (x!=0 or y!=0 or z!=0):
                            self.known_anchors[src]=(x,y,z)
                if utime.ticks_diff(utime.ticks_ms(), last_req) >= 1000:
                    self.comms.send({"type":"REQUEST_MAP","id":self.node_id})
                    last_req = utime.ticks_ms()
                utime.sleep_ms(50)

            utime.sleep_ms(1000)
            self.uwb.flush()
            raw = self.uwb.scan(UWB_SCAN_FRAMES)
            slot_dists = sorted([d for d in raw.values() if d and d > 0])
            others = sorted([n for n in all_nodes if n != self.node_id])
            for i, target in enumerate(others):
                if i < len(slot_dists):
                    dists[str(target)] = round(slot_dists[i], 4)
        except Exception as e:
            print("[Node {}] Range turn error: {}".format(self.node_id, e))
        finally:
            if self.coords:
                self.uwb.configure_warm(self.node_id, role=1)
            else:
                self.uwb.configure(self.node_id, role=1)
            self._oled("ID:{}\nAnch ready".format(self.node_id))

        self.comms.send({"type":"DIST_REPORT","id":self.node_id,"dists":dists})
        self._send_pong()

    # ── Position check ────────────────────────────────────────────────────────

    def _check_position(self):
        if not self.coords or not self.known_anchors: return
        all_dists = []
        try:
            self.uwb.configure_warm(self.node_id, role=0)
            utime.sleep_ms(500)
            self.uwb.flush()
            for _ in range(MOVE_CONFIRM_SCANS):
                raw = self.uwb.scan_with_slots(UWB_QUICK_FRAMES)
                d = sorted([v for v in raw.values() if v and v > 0])
                if d: all_dists.append(d)
                utime.sleep_ms(300)
        except Exception as e:
            print("[Node {}] Check error: {}".format(self.node_id, e))
        finally:
            self.uwb.configure_warm(self.node_id, role=1)
            self._send_pong()
            self._last_pong = utime.ticks_ms()

        if not all_dists: return
        max_slots = max(len(d) for d in all_dists)
        avg_dists = []
        for i in range(max_slots):
            vals = [d[i] for d in all_dists if i < len(d)]
            if vals: avg_dists.append(sum(vals)/len(vals))

        cx,cy,cz = self.coords
        expected = sorted([
            math.sqrt((p[0]-cx)**2+(p[1]-cy)**2+(p[2]-cz)**2)
            for aid,p in self.known_anchors.items() if aid != self.node_id
        ])

        exceeded = 0
        for i,measured in enumerate(avg_dists):
            if i >= len(expected): break
            if abs(measured - expected[i]) > MOVE_THRESHOLD_M:
                exceeded += 1

        if exceeded > 0:
            print("[Node {}] Movement detected — reporting".format(self.node_id))
            others = sorted(self.known_anchors.keys())
            dists = {str(others[i]):round(avg_dists[i],4)
                     for i in range(min(len(others),len(avg_dists)))}
            self.comms.send({"type":"DIST_REPORT","id":self.node_id,"dists":dists})
        else:
            print("[Node {}] Position stable".format(self.node_id))

    # ── Outbound ──────────────────────────────────────────────────────────────

    def _send_pong(self):
        self.comms.send({
            "type":"PONG","id":self.node_id,
            "x":round(self.coords[0],4) if self.coords else 0.0,
            "y":round(self.coords[1],4) if self.coords else 0.0,
            "z":round(self.coords[2],4) if self.coords else 0.0,
        })

    def _broadcast_map(self):
        if not self.coords: return
        self.comms.send({
            "type":"MAP","id":self.node_id,
            "x":round(self.coords[0],4),
            "y":round(self.coords[1],4),
            "z":round(self.coords[2],4),
        })

    # ── OLED ──────────────────────────────────────────────────────────────────

    def _refresh_oled(self):
        coord_str = "COORD" if self._is_coord else "ID:{}".format(self.node_id)
        if self.coords:
            x,y,z = self.coords
            t = "{}\n{:.2f},{:.2f}".format(coord_str, x, y)
        else:
            t = "{}\nWAITING".format(coord_str)
        self._oled(t)

    def _oled(self, text):
        if not self.oled: return
        try: self.oled.display_text(text)
        except: pass

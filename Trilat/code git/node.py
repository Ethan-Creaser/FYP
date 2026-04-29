"""
node.py — Unified Node State Machine
======================================
Every board runs this. Roles are self-assigned:

  BOOT        → listen for existing clusters, join or create
  FOLLOWER    → member of a cluster, UWB anchor
  LEADER      → cluster coordinator, runs ranging and MDS
  MERGING     → two clusters discovered each other, computing transform
  ROVER       → passive listener (id == 99), self-localises when in range

Cluster merge:
  - Leaders hear each other via LoRa CLUSTER heartbeat
  - Higher-ID leader sends MERGE_REQ to lower-ID leader
  - Lower-ID becomes merged leader
  - Bridge nodes (in UWB range of both clusters) do cross-cluster ranging
  - 2D rigid body transform computed to align coordinate frames
  - All positions converted to primary cluster frame
  - Merged map broadcast to all nodes and rover
"""

import utime, math, gc
from machine import reset as hard_reset
from localise import solve_from_distance_matrix

# ── Role IDs ──────────────────────────────────────────────────────────────────
ROVER_ID = 99

# ── Timing ────────────────────────────────────────────────────────────────────
BOOT_LISTEN_MS        = 8_000   # listen for existing clusters on boot
BOOT_PING_MS          = 2_000   # ping interval during boot
ELECTION_DELAY_MS     = 200     # per-ID delay before declaring leader
DISCOVERY_MS          = 15_000  # collect members before ranging
RANGE_TURN_WAIT_MS    = 40_000  # max wait per node during ranging
RANGE_TURN_GAP_MS     = 2_000   # gap between turns
CLUSTER_HB_MS         = 10_000  # cluster heartbeat interval
MEMBER_TIMEOUT_MS     = 90_000  # remove member after this silence
LEADER_TIMEOUT_MS     = 90_000  # declare self leader after this silence
MAP_BROADCAST_MS      = 15_000  # how often to broadcast positions
RANGE_CHECK_MS        = 60_000  # how often followers check position
PONG_INTERVAL_MS      = 10_000  # proactive pong interval
MOVE_THRESHOLD_M      = 0.40    # metres before re-localising
MOVE_CONFIRM_SCANS    = 3       # scans to average for move detection
UWB_SCAN_FRAMES       = 20
UWB_QUICK_FRAMES      = 5
ANCHOR_SETTLE_MS      = 3_000
OLED_REFRESH_MS       = 1_000
MERGE_CROSS_FRAMES    = 10      # frames for cross-cluster ranging
MERGE_TIMEOUT_MS      = 60_000  # abort merge after this


def emit(obj):
    import ujson
    print(ujson.dumps(obj))


class Node:

    def __init__(self, node_id, name, comms, uwb, oled=None):
        self.node_id      = node_id
        self.name         = name
        self.comms        = comms
        self.uwb          = uwb
        self.oled         = oled

        # Role
        self._is_leader   = False
        self._is_rover    = (node_id == ROVER_ID)

        # Cluster state
        self.cluster_id   = None   # leader ID of our cluster
        self.members      = {}     # id → {x,y,z,last_seen} (leader only)
        self.known_nodes  = set()  # all heard node IDs
        self.dist_matrix  = {}     # (a,b) → distance_m

        # Position
        self.coords       = None
        self.known_anchors = {}    # id → (x,y,z)

        # Timers
        self._last_pong   = 0
        self._last_map    = 0
        self._last_check  = 0
        self._last_hb     = 0
        self._last_oled   = 0
        self._last_leader = utime.ticks_ms()
        self._boot_time   = utime.ticks_ms()

    # ── Entry ─────────────────────────────────────────────────────────────────

    def run(self):
        if self._is_rover:
            self._run_rover()
            return

        print("[Node {}] Booting...".format(self.node_id))
        self._oled("ID:{}\nBooting...".format(self.node_id))
        self.uwb.configure(self.node_id, role=1)

        self._phase_boot()

        if self._is_leader:
            self._phase_leader()
        else:
            self._phase_follower()

    # ── Boot phase ────────────────────────────────────────────────────────────

    def _phase_boot(self):
        """
        Listen for existing clusters. Join one or declare self as leader.
        Lower IDs wait less before declaring — natural priority.
        """
        print("[Node {}] Listening for clusters...".format(self.node_id))
        self._oled("ID:{}\nListening...".format(self.node_id))

        # Stagger boot by ID to reduce simultaneous elections
        utime.sleep_ms(self.node_id * ELECTION_DELAY_MS)

        deadline = utime.ticks_add(utime.ticks_ms(), BOOT_LISTEN_MS)
        last_ping = utime.ticks_add(utime.ticks_ms(), -BOOT_PING_MS)

        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            now = utime.ticks_ms()

            if utime.ticks_diff(now, last_ping) >= BOOT_PING_MS:
                self.comms.send({"type":"PING","id":self.node_id})
                last_ping = utime.ticks_ms()

            msg = self.comms.recv()
            if msg:
                t = msg.get("type"); nid = msg.get("id")
                if t == "CLUSTER":
                    # An existing cluster is running — join it
                    leader = msg.get("leader")
                    if leader is not None:
                        print("[Node {}] Joining cluster led by {}".format(
                            self.node_id, leader))
                        self.cluster_id = leader
                        self._is_leader = False
                        self._last_leader = utime.ticks_ms()
                        self._oled("ID:{}\nJoin cl:{}".format(self.node_id, leader))
                        # Announce ourselves to the cluster
                        self.comms.send({"type":"PONG","id":self.node_id,
                                         "x":0.0,"y":0.0,"z":0.0})
                        return
                elif t in ("PONG","PING") and nid and nid != self.node_id:
                    self.known_nodes.add(nid)

            utime.sleep_ms(50)

        # No cluster found — declare self as leader
        print("[Node {}] No cluster found — declaring self leader".format(
            self.node_id))
        self._is_leader  = True
        self.cluster_id  = self.node_id
        self.members[self.node_id] = {
            "x":0.0,"y":0.0,"z":0.0,"last_seen":utime.ticks_ms()
        }
        self.coords = (0.0, 0.0, 0.0)
        self.known_anchors[self.node_id] = (0.0, 0.0, 0.0)
        self.comms.send({"type":"CLUSTER","id":self.node_id,
                         "leader":self.node_id,"members":[]})
        emit({"type":"MAP","id":self.node_id,
              "x":0.0,"y":0.0,"z":0.0,"rssi":0,"snr":0.0})
        self._oled("LEADER\nID:{}".format(self.node_id))

    # ── Leader phase ──────────────────────────────────────────────────────────

    def _phase_leader(self):
        """
        Leader: discover members, run ranging, solve, then maintain network.
        """
        # Discover members
        self._leader_discover()
        # Range and solve
        self._leader_range_and_solve()
        # Steady state
        self._leader_steady()

    def _leader_discover(self):
        """Collect all members before ranging."""
        print("[Node {}] (leader) Discovering members...".format(self.node_id))
        self._oled("LEADER\nDiscovery")

        deadline = utime.ticks_add(utime.ticks_ms(), DISCOVERY_MS)
        last_ping = utime.ticks_add(utime.ticks_ms(), -BOOT_PING_MS)

        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            now = utime.ticks_ms()
            if utime.ticks_diff(now, last_ping) >= BOOT_PING_MS:
                self.comms.send({"type":"CLUSTER","id":self.node_id,
                                 "leader":self.node_id,
                                 "members":list(self.members.keys())})
                self.comms.send({"type":"PING","id":self.node_id})
                last_ping = utime.ticks_ms()

            msg = self.comms.recv()
            if msg:
                t = msg.get("type"); nid = msg.get("id")
                if t in ("PONG","PING") and nid and nid != self.node_id:
                    if nid not in self.members:
                        print("[Node {}] (leader) Member joined: {}".format(
                            self.node_id, nid))
                        self.members[nid] = {
                            "x":0.0,"y":0.0,"z":0.0,
                            "last_seen":utime.ticks_ms()
                        }
                    else:
                        self.members[nid]["last_seen"] = utime.ticks_ms()
                    self.known_nodes.add(nid)
            utime.sleep_ms(50)

        print("[Node {}] (leader) Members: {}".format(
            self.node_id, list(self.members.keys())))

    def _leader_range_and_solve(self):
        """Round-robin ranging then MDS solve."""
        all_nodes = sorted(self.members.keys())
        if len(all_nodes) < 2:
            print("[Node {}] (leader) Only 1 node — skipping ranging".format(
                self.node_id))
            return

        print("[Node {}] (leader) Starting ranging: {}".format(
            self.node_id, all_nodes))
        self._oled("LEADER\nRanging...")

        for turn_node in all_nodes:
            # Ping between turns to catch late nodes
            self.comms.send({"type":"PING","id":self.node_id})
            drain = utime.ticks_add(utime.ticks_ms(), 2000)
            while utime.ticks_diff(drain, utime.ticks_ms()) > 0:
                msg = self.comms.recv()
                if msg and msg.get("type") == "PONG":
                    nid = msg.get("id")
                    if nid and nid != self.node_id and nid not in self.members:
                        self.members[nid] = {
                            "x":0.0,"y":0.0,"z":0.0,
                            "last_seen":utime.ticks_ms()
                        }
                        all_nodes = sorted(self.members.keys())
                        print("[Node {}] (leader) Late member: {}".format(
                            self.node_id, nid))
                utime.sleep_ms(50)

            self._oled("LEADER\nRange:{}".format(turn_node))

            if turn_node == self.node_id:
                self._do_own_ranging(all_nodes)
            else:
                self.comms.send({"type":"RANGE_TURN","id":turn_node,
                                 "nodes":all_nodes})
                deadline = utime.ticks_add(utime.ticks_ms(), RANGE_TURN_WAIT_MS)
                while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
                    msg = self.comms.recv()
                    if msg:
                        t = msg.get("type"); nid = msg.get("id")
                        if t == "DIST_REPORT" and nid == turn_node:
                            self._record_dist_report(msg)
                            break
                        elif t == "PONG" and nid and nid != self.node_id:
                            if nid in self.members:
                                self.members[nid]["last_seen"] = utime.ticks_ms()
                    utime.sleep_ms(50)

            utime.sleep_ms(RANGE_TURN_GAP_MS)
            gc.collect()

        self._solve_and_broadcast()

    def _do_own_ranging(self, all_nodes):
        """Leader switches to tag and ranges all members."""
        try:
            self.uwb.configure_warm(self.node_id, role=0)
            utime.sleep_ms(2000)
            self.uwb.flush()
            raw = self.uwb.scan(UWB_SCAN_FRAMES)
            dists = sorted([d for d in raw.values() if d and d > 0])
            others = [n for n in all_nodes if n != self.node_id]
            for i, nid in enumerate(others):
                if i < len(dists):
                    self.dist_matrix[(self.node_id, nid)] = dists[i]
                    print("[Node {}] d({},{})={:.3f}m".format(
                        self.node_id, self.node_id, nid, dists[i]))
        except Exception as e:
            print("[Node {}] Own ranging error: {}".format(self.node_id, e))
        finally:
            self.uwb.configure(self.node_id, role=1)

    def _record_dist_report(self, msg):
        src = msg.get("id")
        dists = msg.get("dists", {})
        if src in self.members:
            self.members[src]["last_seen"] = utime.ticks_ms()
        for target_str, d in dists.items():
            try:
                target = int(target_str)
                if target != src and d and d > 0:
                    self.dist_matrix[(src, target)] = float(d)
            except: pass

    def _solve_and_broadcast(self):
        """Run MDS and broadcast all positions."""
        all_nodes = sorted(self.members.keys())
        if len(all_nodes) < 2:
            return
        try:
            positions = solve_from_distance_matrix(all_nodes, self.dist_matrix)
        except Exception as e:
            print("[Node {}] MDS error: {}".format(self.node_id, e))
            return

        now = utime.ticks_ms()
        for nid, (x, y, z) in positions.items():
            self.members[nid].update({"x":round(x,4),"y":round(y,4),"z":round(z,4)})
            self.known_anchors[nid] = (x, y, z)
            if nid == self.node_id:
                self.coords = (x, y, z)
            emit({"type":"MAP","id":nid,"x":round(x,4),"y":round(y,4),
                  "z":round(z,4),"rssi":0,"snr":0.0})

        # Broadcast positions to all members
        for nid, e in self.members.items():
            self.comms.send({"type":"MAP","id":nid,
                             "x":e["x"],"y":e["y"],"z":e["z"]})
            utime.sleep_ms(100)

        # Active ping phase after solve — announce to any nearby nodes
        # that missed discovery (edge nodes, late booters, rover)
        print("[Node {}] (leader) Post-solve ping phase...".format(self.node_id))
        ping_end = utime.ticks_add(utime.ticks_ms(), 10_000)
        while utime.ticks_diff(ping_end, utime.ticks_ms()) > 0:
            self.comms.send({"type":"PING","id":self.node_id})
            utime.sleep_ms(500)
            msg = self.comms.recv()
            if msg and msg.get("type") == "PONG":
                nid = msg.get("id")
                if nid and nid != self.node_id and nid not in self.members:
                    print("[Node {}] (leader) Post-solve: new node {}".format(
                        self.node_id, nid))
                    self.members[nid] = {
                        "x":0.0,"y":0.0,"z":0.0,
                        "last_seen":utime.ticks_ms()
                    }
                    self.known_nodes.add(nid)

        print("[Node {}] (leader) Solved {} nodes".format(
            self.node_id, len(positions)))

    def _leader_steady(self):
        """Leader steady state — heartbeat, handle new nodes, watch for merges."""
        self._oled("LEADER:{}\nN:{}".format(self.node_id, len(self.members)))
        self.comms.send({"type":"CLUSTER","id":self.node_id,
                         "leader":self.node_id,
                         "members":list(self.members.keys())})
        print("[Node {}] (leader) Entering steady state".format(self.node_id))

        while True:
            now = utime.ticks_ms()
            msg = self.comms.recv()

            if msg:
                t = msg.get("type"); nid = msg.get("id")

                if nid and nid != self.node_id:
                    self.known_nodes.add(nid)

                if t == "PONG" and nid and nid != self.node_id:
                    if nid in self.members:
                        self.members[nid]["last_seen"] = utime.ticks_ms()
                    elif nid not in self.known_nodes:
                        # New node — run mini ranging
                        self.members[nid] = {
                            "x":0.0,"y":0.0,"z":0.0,
                            "last_seen":utime.ticks_ms()
                        }
                        self._mini_range(nid)

                elif t == "MAP" and nid and nid != self.node_id:
                    if nid in self.members:
                        self.members[nid].update({
                            "x":float(msg.get("x",0)),
                            "y":float(msg.get("y",0)),
                            "z":float(msg.get("z",0)),
                            "last_seen":utime.ticks_ms()
                        })

                elif t == "DIST_REPORT" and nid and nid != self.node_id:
                    self._record_dist_report(msg)
                    self._solve_and_broadcast()

                elif t == "NEED_TAG" and nid and nid != self.node_id:
                    self._mini_range(nid)

                elif t == "REQUEST_MAP":
                    self._broadcast_all()

                elif t == "CLUSTER" and nid and nid != self.node_id:
                    other_leader = msg.get("leader")
                    if other_leader is not None and other_leader != self.node_id:
                        # Another cluster found — initiate merge
                        self._handle_merge(other_leader, nid)

                elif t == "MERGE_REQ" and nid and nid != self.node_id:
                    # We are the primary (lower ID) — accept merge
                    self._accept_merge(msg)

                elif t == "MERGE_POSITIONS":
                    # Secondary cluster positions arriving — integrate
                    self._integrate_merge(msg)

            # Cluster heartbeat
            if utime.ticks_diff(now, self._last_hb) >= CLUSTER_HB_MS:
                self._do_cluster_heartbeat()
                self._last_hb = utime.ticks_ms()

            # Broadcast positions
            if utime.ticks_diff(now, self._last_map) >= MAP_BROADCAST_MS:
                self._broadcast_all()
                self._last_map = utime.ticks_ms()

            if utime.ticks_diff(now, self._last_oled) >= OLED_REFRESH_MS:
                self._oled("LEADER:{}\nN:{}".format(self.node_id, len(self.members)))
                self._last_oled = utime.ticks_ms()

            gc.collect()
            utime.sleep_ms(50)

    def _do_cluster_heartbeat(self):
        self.comms.send({"type":"CLUSTER","id":self.node_id,
                         "leader":self.node_id,
                         "members":list(self.members.keys())})
        now = utime.ticks_ms()
        for nid in list(self.members.keys()):
            if nid == self.node_id: continue
            last = self.members[nid].get("last_seen", 0)
            if utime.ticks_diff(now, last) > MEMBER_TIMEOUT_MS:
                print("[Node {}] (leader) Member {} offline".format(
                    self.node_id, nid))
                del self.members[nid]
                self.known_anchors.pop(nid, None)
                emit({"type":"OFFLINE","id":nid})

    def _mini_range(self, new_nid):
        all_nodes = sorted(self.members.keys())
        self.comms.send({"type":"RANGE_TURN","id":new_nid,"nodes":all_nodes})
        deadline = utime.ticks_add(utime.ticks_ms(), RANGE_TURN_WAIT_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            msg = self.comms.recv()
            if msg and msg.get("type") == "DIST_REPORT" and msg.get("id") == new_nid:
                self._record_dist_report(msg)
                self._solve_and_broadcast()
                break
            utime.sleep_ms(50)

    def _broadcast_all(self):
        self._last_map = utime.ticks_ms()
        self.comms.send({"type":"CLUSTER","id":self.node_id,
                         "leader":self.node_id,
                         "members":list(self.members.keys())})
        for nid, e in self.members.items():
            self.comms.send({"type":"MAP","id":nid,
                             "x":e["x"],"y":e["y"],"z":e["z"]})
            emit({"type":"MAP","id":nid,
                  "x":e["x"],"y":e["y"],"z":e["z"],"rssi":0,"snr":0.0})
            utime.sleep_ms(80)

    # ── Cluster merge ─────────────────────────────────────────────────────────

    def _handle_merge(self, other_leader, other_node):
        """Called when we (leader) discover another cluster."""
        if other_leader == self.node_id:
            return
        if other_leader < self.node_id:
            # Other cluster is primary — send them merge request
            print("[Node {}] Sending MERGE_REQ to leader {}".format(
                self.node_id, other_leader))
            self.comms.send({
                "type":    "MERGE_REQ",
                "id":      self.node_id,
                "leader":  self.node_id,
                "members": list(self.members.keys()),
            })
        # If other_leader > self.node_id, we are primary — wait for their MERGE_REQ

    def _accept_merge(self, msg):
        """
        We are the primary leader (lower ID). Accept the secondary cluster.
        Find bridge nodes and compute coordinate transform.
        """
        sec_leader  = msg.get("leader")
        sec_members = msg.get("members", [])
        print("[Node {}] (leader) Accepting merge from cluster {}".format(
            self.node_id, sec_leader))
        self._oled("MERGING\ncl:{}".format(sec_leader))

        # Tell secondary cluster members to do cross-cluster ranging
        # Bridge = any primary member that can UWB-range a secondary member
        # We ask our members to range as tag — secondary members are already anchors
        cross_dists = {}

        for my_nid in sorted(self.members.keys()):
            self.comms.send({"type":"CROSS_RANGE","id":my_nid,
                             "targets":sec_members})
            deadline = utime.ticks_add(utime.ticks_ms(), RANGE_TURN_WAIT_MS)
            while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
                msg2 = self.comms.recv()
                if msg2 and msg2.get("type") == "CROSS_REPORT" and msg2.get("id") == my_nid:
                    for target_str, d in msg2.get("dists",{}).items():
                        try:
                            target = int(target_str)
                            cross_dists[(my_nid, target)] = float(d)
                        except: pass
                    break
                utime.sleep_ms(50)

        if not cross_dists:
            print("[Node {}] No cross-cluster distances — merge failed".format(
                self.node_id))
            return

        # Request secondary cluster positions
        self.comms.send({"type":"REQUEST_POSITIONS","id":self.node_id,
                         "target_leader":sec_leader})

    def _integrate_merge(self, msg):
        """
        Receive secondary cluster positions and cross distances,
        compute transform, add to our map.
        """
        sec_positions = msg.get("positions", {})  # {str(id): [x,y,z]}
        cross_dists   = msg.get("cross_dists", {})  # {str(a)+","+str(b): d}

        if not sec_positions or not cross_dists:
            return

        # Parse cross distances
        parsed_cross = {}
        for key, d in cross_dists.items():
            try:
                a, b = [int(x) for x in key.split(",")]
                parsed_cross[(a,b)] = float(d)
            except: pass

        # Get our bridge node positions (primary cluster)
        my_bridges  = {a for (a,b) in parsed_cross.keys() if a in self.members}
        sec_bridges = {b for (a,b) in parsed_cross.keys()}

        if len(my_bridges) < 1 or len(sec_bridges) < 1:
            print("[Node {}] Not enough bridge nodes for transform".format(
                self.node_id))
            return

        # Compute transform: translate + rotate secondary into primary frame
        # Use the bridge node distances to anchor the secondary cluster
        # Build a combined distance matrix and re-solve with MDS
        all_ids = sorted(list(self.members.keys()) +
                         [int(k) for k in sec_positions.keys()])
        combined_matrix = dict(self.dist_matrix)

        # Add cross-cluster distances
        for (a,b),d in parsed_cross.items():
            combined_matrix[(a,b)] = d
            combined_matrix[(b,a)] = d  # symmetric

        # Add intra-secondary distances if available
        # (secondary leader should send their dist_matrix too)
        sec_dist_matrix = msg.get("dist_matrix", {})
        for key, d in sec_dist_matrix.items():
            try:
                a, b = [int(x) for x in key.split(",")]
                combined_matrix[(a,b)] = float(d)
            except: pass

        try:
            positions = solve_from_distance_matrix(all_ids, combined_matrix)
        except Exception as e:
            print("[Node {}] Merge MDS error: {}".format(self.node_id, e))
            return

        # Fix coordinate frame — our primary node stays at origin
        primary_origin = self.node_id
        ox, oy, _ = positions.get(primary_origin, (0,0,0))
        positions = {nid: (round(x-ox,4), round(y-oy,4), 0.0)
                     for nid,(x,y,z) in positions.items()}

        # Update our map with all nodes
        now = utime.ticks_ms()
        for nid, (x,y,z) in positions.items():
            self.members[nid] = {"x":x,"y":y,"z":z,"last_seen":now}
            self.known_anchors[nid] = (x,y,z)
            if nid == self.node_id:
                self.coords = (x,y,z)
            emit({"type":"MAP","id":nid,"x":x,"y":y,"z":z,"rssi":0,"snr":0.0})

        # Broadcast merged map to all nodes
        for nid, e in self.members.items():
            self.comms.send({"type":"MAP","id":nid,
                             "x":e["x"],"y":e["y"],"z":e["z"]})
            utime.sleep_ms(80)

        print("[Node {}] Merge complete — {} total nodes".format(
            self.node_id, len(self.members)))
        self._oled("MERGED\nN:{}".format(len(self.members)))

    # ── Follower phase ────────────────────────────────────────────────────────

    def _phase_follower(self):
        """Follower: anchor, respond to leader commands, watch for leader loss."""
        print("[Node {}] Follower of cluster {}".format(
            self.node_id, self.cluster_id))
        self._oled("ID:{}\nFOLLOWER".format(self.node_id))

        while True:
            now = utime.ticks_ms()
            msg = self.comms.recv()

            if msg:
                t = msg.get("type"); nid = msg.get("id")

                # Track leader liveness
                if nid == self.cluster_id and t in ("CLUSTER","HEARTBEAT","MAP"):
                    self._last_leader = utime.ticks_ms()

                if t == "PING":
                    self._send_pong()

                elif t == "CLUSTER":
                    leader = msg.get("leader")
                    if leader is not None:
                        self.cluster_id = leader
                        self._last_leader = utime.ticks_ms()

                elif t == "MAP":
                    if nid is not None:
                        x=float(msg.get("x",0)); y=float(msg.get("y",0)); z=float(msg.get("z",0))
                        self.known_anchors[nid] = (x,y,z)
                        if nid == self.node_id:
                            self.coords = (x,y,z)
                            print("[Node {}] Position: ({:.3f},{:.3f},{:.3f})".format(
                                self.node_id,x,y,z))
                            # Ping for 5s to announce to nearby nodes
                            ping_end = utime.ticks_add(utime.ticks_ms(), 5_000)
                            while utime.ticks_diff(ping_end, utime.ticks_ms()) > 0:
                                self.comms.send({"type":"PONG",
                                    "id":self.node_id,
                                    "x":round(x,4),"y":round(y,4),"z":round(z,4)})
                                utime.sleep_ms(500)

                elif t == "RANGE_TURN" and nid == self.node_id:
                    self._do_range_turn(msg.get("nodes",[]))

                elif t == "CROSS_RANGE" and nid == self.node_id:
                    self._do_cross_range(msg.get("targets",[]))

                elif t == "REQUEST_MAP":
                    if self.coords: self._broadcast_map()

            # Proactive pong
            if utime.ticks_diff(now, self._last_pong) >= PONG_INTERVAL_MS:
                self._send_pong()
                self._last_pong = utime.ticks_ms()

            # Periodic MAP broadcast
            if self.coords and utime.ticks_diff(now, self._last_map) >= MAP_BROADCAST_MS:
                self._broadcast_map()
                self._last_map = utime.ticks_ms()

            # Position check
            if (self.coords and
                    utime.ticks_diff(now, self._last_check) >= RANGE_CHECK_MS):
                self._check_position()
                self._last_check = utime.ticks_ms()

            # Leader watchdog
            leader_age = utime.ticks_diff(now, self._last_leader)
            if leader_age > LEADER_TIMEOUT_MS and self.coords:
                print("[Node {}] Leader {} gone — considering takeover".format(
                    self.node_id, self.cluster_id))
                self._consider_takeover()

            if utime.ticks_diff(now, self._last_oled) >= OLED_REFRESH_MS:
                self._refresh_oled()
                self._last_oled = utime.ticks_ms()

            gc.collect()
            utime.sleep_ms(50)

    def _consider_takeover(self):
        """Become leader if no lower-ID active node exists."""
        now = utime.ticks_ms()
        # Check if any lower-ID node is still active
        active_lower = [n for n in self.known_nodes
                        if n < self.node_id and n != self.cluster_id]
        # Can't easily check last_seen for followers — just wait proportional to ID
        utime.sleep_ms(self.node_id * 1000)
        # Check for new CLUSTER message
        msg = self.comms.recv()
        if msg and msg.get("type") == "CLUSTER":
            self._last_leader = utime.ticks_ms()
            self.cluster_id = msg.get("leader", self.cluster_id)
            return
        # Take over
        print("[Node {}] Taking over as leader".format(self.node_id))
        self._is_leader  = True
        self.cluster_id  = self.node_id
        self._last_leader = utime.ticks_ms()
        # Rebuild members from known_anchors
        for nid, pos in self.known_anchors.items():
            self.members[nid] = {"x":pos[0],"y":pos[1],"z":pos[2],
                                 "last_seen":utime.ticks_ms()}
        self.comms.send({"type":"CLUSTER","id":self.node_id,
                         "leader":self.node_id,
                         "members":list(self.members.keys())})
        emit({"type":"COORD","id":self.node_id,"coord_id":self.node_id})
        self._oled("LEADER:{}\nTakeover".format(self.node_id))
        self._leader_steady()

    def _do_range_turn(self, all_nodes):
        """Switch to tag, range all others, send DIST_REPORT."""
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

    def _do_cross_range(self, targets):
        """Range against nodes from another cluster for merge transform."""
        self._oled("ID:{}\nCross range".format(self.node_id))
        dists = {}
        try:
            self.uwb.configure_warm(self.node_id, role=0)
            utime.sleep_ms(2000)
            self.uwb.flush()
            raw = self.uwb.scan(MERGE_CROSS_FRAMES)
            slot_dists = sorted([d for d in raw.values() if d and d > 0])
            for i, target in enumerate(sorted(targets)):
                if i < len(slot_dists):
                    dists[str(target)] = round(slot_dists[i], 4)
        except Exception as e:
            print("[Node {}] Cross range error: {}".format(self.node_id, e))
        finally:
            self.uwb.configure_warm(self.node_id, role=1)
            self._oled("ID:{}\nAnch ready".format(self.node_id))

        self.comms.send({"type":"CROSS_REPORT","id":self.node_id,"dists":dists})

    def _check_position(self):
        """Quick check — average scans, re-report if moved."""
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

        exceeded = sum(1 for i,m in enumerate(avg_dists)
                       if i < len(expected) and abs(m-expected[i]) > MOVE_THRESHOLD_M)

        if exceeded > 0:
            others = sorted(self.known_anchors.keys())
            dists = {str(others[i]):round(avg_dists[i],4)
                     for i in range(min(len(others),len(avg_dists)))}
            self.comms.send({"type":"DIST_REPORT","id":self.node_id,"dists":dists})
            print("[Node {}] Movement reported".format(self.node_id))
        else:
            print("[Node {}] Position stable".format(self.node_id))

    # ── Rover ─────────────────────────────────────────────────────────────────

    def _run_rover(self):
        """Passive listener — forwards all MAP messages to laptop."""
        print("[Rover {}] Started".format(self.node_id))
        self._oled("ROVER\nListening...")
        self.uwb.configure(self.node_id, role=0)
        self.comms.send({"type":"REQUEST_MAP","id":self.node_id})
        last_req = utime.ticks_ms()
        last_loc = utime.ticks_ms()

        while True:
            now = utime.ticks_ms()
            msg = self.comms.recv()
            if msg:
                t = msg.get("type"); nid = msg.get("id")
                if t == "MAP" and nid is not None:
                    x=float(msg.get("x",0)); y=float(msg.get("y",0)); z=float(msg.get("z",0))
                    self.known_anchors[nid] = (x,y,z)
                    emit({"type":"MAP","id":nid,"x":round(x,4),"y":round(y,4),
                          "z":round(z,4),"rssi":self.comms.rssi(),
                          "snr":round(self.comms.snr(),2)})
                elif t == "OFFLINE":
                    self.known_anchors.pop(nid, None)
                    emit({"type":"OFFLINE","id":nid})
                elif t in ("CLUSTER","HEARTBEAT"):
                    self.comms.send({"type":"PONG","id":self.node_id,
                                     "x":round(self.coords[0],4) if self.coords else 0.0,
                                     "y":round(self.coords[1],4) if self.coords else 0.0,
                                     "z":round(self.coords[2],4) if self.coords else 0.0})

            if utime.ticks_diff(now, last_req) >= 10_000:
                # PING to trigger PONG from any nearby nodes
                # REQUEST_MAP to get positions from localised nodes
                self.comms.send({"type":"PING","id":self.node_id})
                utime.sleep_ms(200)
                self.comms.send({"type":"REQUEST_MAP","id":self.node_id})
                last_req = utime.ticks_ms()

            if (len(self.known_anchors) >= 2 and
                    utime.ticks_diff(now, last_loc) >= 20_000):
                self._rover_localise()
                last_loc = utime.ticks_ms()

            if utime.ticks_diff(now, self._last_oled) >= 2000:
                self._oled("ROVER\nAnch:{}".format(len(self.known_anchors)))
                self._last_oled = utime.ticks_ms()

            gc.collect()
            utime.sleep_ms(50)

    def _rover_localise(self):
        """Rover self-localises against visible anchors."""
        try:
            self.uwb.flush()
            raw = self.uwb.scan(10)
            slot_dists = sorted([d for d in raw.values() if d and d > 0])
            if not slot_dists: return

            if self.coords:
                cx,cy,_ = self.coords
                anchors_sorted = sorted(
                    self.known_anchors.items(),
                    key=lambda kv: math.sqrt((kv[1][0]-cx)**2+(kv[1][1]-cy)**2))
            else:
                anchors_sorted = sorted(self.known_anchors.items())

            measurements = []
            for i,(aid,apos) in enumerate(anchors_sorted):
                if i >= len(slot_dists): break
                measurements.append((apos, slot_dists[i]))

            if not measurements: return

            from localise import solve_position
            result = solve_position(measurements, self.coords)
            if result is None: return

            x,y,z = result
            self.coords = (x,y,z)
            emit({"type":"MAP","id":self.node_id,"x":round(x,4),
                  "y":round(y,4),"z":round(z,4),"rssi":0,"snr":0.0})
            print("[Rover] Position: ({:.3f},{:.3f})".format(x,y))
        except Exception as e:
            print("[Rover] Localise error:", e)

    # ── Helpers ───────────────────────────────────────────────────────────────

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

    def _refresh_oled(self):
        role = "LEADER" if self._is_leader else "FOLLWR"
        if self.coords:
            x,y,_ = self.coords
            t = "{}:{}\n{:.2f},{:.2f}".format(role,self.node_id,x,y)
        else:
            t = "{}:{}\nWAITING".format(role,self.node_id)
        self._oled(t)

    def _oled(self, text):
        if not self.oled: return
        try: self.oled.display_text(text)
        except: pass

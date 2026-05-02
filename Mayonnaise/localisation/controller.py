import gc
import utime

from localisation.ranging import UWBRanger
from localisation.solver import solve_from_distance_matrix
from packets import (
    BROADCAST,
    LOCALISE_DISCOVERY,
    LOCALISE_POSITION,
    LOCALISE_RESULT,
    LOCALISE_START,
    LOCALISE_TURN,
    make_packet,
)

ROLE_FIELD_EGG = "field_egg"


class LocalisationController:
    """
    Localisation state machine for a field egg node.

    Phases:
      IDLE        — not yet started
      SETTLING    — passive wait: mesh HELLO traffic populates node_directory;
                    UWB configured as anchor; no repeated broadcasts needed.
      COORDINATOR — running round-robin UWB ranging and solving positions
      WAITING     — follower waiting for LOCALISE_TURN then LOCALISE_POSITION
      STEADY      — localisation complete

    Discovery uses the existing HELLO/HEARTBEAT mesh rather than a separate
    broadcast loop.  When the settle window expires the controller reads
    node_directory (kept current by EggNode from every incoming HELLO) to
    build the member list and elects a coordinator (lowest node_id wins).

    The coordinator broadcasts LOCALISE_START with the definitive member list,
    then runs full round-robin UWB ranging so every pair of nodes has a real
    measured distance before MDS solves the positions.
    """

    STATE_IDLE        = "idle"
    STATE_SETTLING    = "settling"
    STATE_COORDINATOR = "coordinator"
    STATE_WAITING     = "waiting_for_map"
    STATE_STEADY      = "steady"

    def __init__(
        self,
        config,
        uwb,
        logger,
        node_id,
        node_name,
        uwb_id,
        uwb_channel,
        uwb_rate,
        send_packet_fn,
        next_seq_fn,
        poll_radio_fn,
        neighbours,
        node_positions,
        node_directory,
        set_self_pos_fn,
        on_complete_fn,
    ):
        self.logger    = logger
        self.node_id   = node_id
        self.node_name = node_name

        self.ranger = UWBRanger(
            uwb       = uwb,
            logger    = logger,
            uwb_id    = uwb_id,
            channel   = uwb_channel,
            rate      = uwb_rate,
            frames    = config.get("localisation_frames",     20),
            settle_ms = config.get("localisation_settle_ms", 3000),
        )

        self._send_packet  = send_packet_fn
        self._next_seq     = next_seq_fn
        self._poll_radio   = poll_radio_fn
        self._neighbours   = neighbours
        self._positions    = node_positions
        self._directory    = node_directory
        self._set_self_pos = set_self_pos_fn
        self._on_complete  = on_complete_fn

        self.settle_ms   = config.get("localisation_boot_ms",   8000)
        self.turn_ms     = config.get("localisation_turn_ms",  40000)
        self.max_members = min(config.get("localisation_max_members", 8), 8)
        # 700 ms > SF9 BW125 packet airtime (~660 ms) so staggered sends don't collide.
        self._discovery_jitter_ms = config.get("localisation_discovery_jitter_ms", 700)

        self.state       = self.STATE_IDLE
        self.is_complete = False

        self._members        = {}
        self._coordinator    = None
        self._dist_matrix    = {}
        self._results        = {}
        self._expected_total = 0
        self._deadline       = None
        self._discovery_due  = None
        self._discovery_sent = False

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    def start(self, now, reason="boot"):
        """
        Begin settling phase.  Configures UWB as anchor (cold on first boot,
        warm thereafter) then waits settle_ms for HELLO traffic to populate
        node_directory before electing a coordinator.
        """
        self.state       = self.STATE_SETTLING
        self.is_complete = False
        self._members        = {}
        self._coordinator    = None
        self._dist_matrix    = {}
        self._results        = {}
        self._expected_total = 0
        self._discovery_due  = None
        self._discovery_sent = False

        for k in list(self._positions):
            del self._positions[k]

        self.logger.event("LOCALISATION START", [
            ("Reason", reason), ("Node", self.node_id), ("UWB", self.ranger.uwb_id),
        ])

        self.ranger.set_anchor(cold=(reason == "boot"))

        # Re-snapshot after UWB configure (blocks ~8 s on cold boot).
        post_uwb = utime.ticks_ms()
        self._deadline = utime.ticks_add(post_uwb, self.settle_ms)
        self.logger.event("LOCALISATION READY", [
            ("UWB ms",    utime.ticks_diff(post_uwb, now)),
            ("Settle ms", self.settle_ms),
        ])

        # Schedule the discovery broadcast non-blocking so the poll loop can
        # receive other nodes' discoveries during our own jitter window.
        # Stagger by node_id * jitter_ms (>= LoRa airtime) to prevent collisions.
        jitter_ms = min(self.node_id * self._discovery_jitter_ms, 4000)
        self._discovery_due = utime.ticks_add(post_uwb, jitter_ms)

    def advance(self, now):
        """Called every poll cycle while not steady."""
        if self.state == self.STATE_SETTLING:
            self._phase_settling(now)
        elif self.state == self.STATE_WAITING:
            self._phase_waiting(now)

    # ------------------------------------------------------------------ #
    # Packet handlers — called by EggNode                                 #
    # ------------------------------------------------------------------ #

    def handle_discovery(self, packet, now):
        """One-shot trigger: peer has entered settling and wants us to join."""
        src = packet.get("src")
        if self.state == self.STATE_STEADY and src not in self._positions:
            self.logger.event("DISC TRIGGERED RELOCALISE", [("From", src)])
            self.start(now, reason="new_peer")

    def handle_start(self, packet, now):
        """
        Coordinator has elected the definitive member list.
        Followers switch to WAITING and prepare for LOCALISE_TURN.
        """
        payload     = packet.get("p", {})
        coordinator = payload.get("coordinator")
        if coordinator is None or coordinator == self.node_id:
            return

        if self.state == self.STATE_STEADY:
            self.start(now, reason="new_round")
        elif self.state not in (self.STATE_SETTLING, self.STATE_WAITING):
            return

        self._coordinator = coordinator
        self._members = {}
        for m in payload.get("members", []):
            nid    = m.get("node_id")
            uwb_id = m.get("uwb_id")
            if nid is None or uwb_id is None:
                continue
            self._members[nid] = {
                "node_id":   nid,
                "name":      m.get("name", "egg_{}".format(nid)),
                "uwb_id":    int(uwb_id),
                "last_seen": now,
            }

        self.state = self.STATE_WAITING
        wait_budget = max(len(self._members), 2) * self.turn_ms
        self._deadline = utime.ticks_add(now, wait_budget)
        self.logger.event("LOCALISATION FOLLOWER", [
            ("Coordinator", coordinator),
            ("Members",     self._member_ids(self._member_list())),
        ])

    def handle_turn(self, packet, now):
        payload     = packet.get("p", {})
        coordinator = payload.get("coordinator")
        if coordinator is None:
            return
        self._coordinator = coordinator
        self.logger.event("LOCALISE TURN", [
            ("Coordinator", coordinator), ("Node", self.node_id),
        ])
        self._phase_follower_turn(payload.get("members", []), coordinator, now)

    def handle_result(self, packet, now):
        if self._coordinator != self.node_id:
            return
        payload = packet.get("p", {})
        if payload.get("coordinator") != self.node_id:
            return
        self._record_result(packet.get("src"), payload.get("d", {}), now)

    def handle_position(self, packet, now):
        payload     = packet.get("p", {})
        coordinator = payload.get("coordinator")
        node_id     = payload.get("node_id")
        if node_id is None:
            return

        if coordinator is not None and self._coordinator is None:
            self._coordinator = coordinator
        if (self.state == self.STATE_WAITING
                and coordinator is not None
                and self._coordinator is not None
                and coordinator != self._coordinator):
            return

        try:
            pos = (
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                float(payload.get("z", 0.0)),
            )
        except Exception:
            return

        self._positions[node_id] = pos
        if node_id == self.node_id:
            self._set_self_pos(pos)

        total = payload.get("total")
        if total is not None:
            self._expected_total = int(total)

        if (self.state == self.STATE_WAITING
                and self._expected_total
                and len(self._positions) >= self._expected_total):
            self._finish(now, "map received")

    # ------------------------------------------------------------------ #
    # Settling phase                                                       #
    # ------------------------------------------------------------------ #

    def _phase_settling(self, now):
        # Send discovery at the scheduled time (non-blocking stagger).
        if (not self._discovery_sent
                and self._discovery_due is not None
                and utime.ticks_diff(now, self._discovery_due) >= 0):
            self._send_discovery(now)
            self._discovery_sent = True

        if self._deadline is None or utime.ticks_diff(now, self._deadline) < 0:
            return

        # Build member list from node_directory (populated by HELLO traffic).
        self._build_members_from_directory(now)
        members = self._member_list()

        if len(members) <= 1:
            pos = (0.0, 0.0, 0.0)
            self._positions[self.node_id] = pos
            self._set_self_pos(pos)
            self._finish(now, "solo node")
            return

        self._coordinator = min(m["node_id"] for m in members)
        if self._coordinator == self.node_id:
            self._phase_coordinator(now, members)
        else:
            self.state = self.STATE_WAITING
            wait_budget = max(len(members), 2) * self.turn_ms
            self._deadline = utime.ticks_add(now, wait_budget)
            self.logger.event("LOCALISATION FOLLOWER WAIT", [
                ("Coordinator", self._coordinator),
                ("Known peers", self._member_ids(members)),
            ])

    def _build_members_from_directory(self, now):
        """Populate _members from EggNode.node_directory (HELLO-based discovery)."""
        self._members = {
            self.node_id: {
                "node_id":   self.node_id,
                "name":      self.node_name,
                "uwb_id":    self.ranger.uwb_id,
                "last_seen": now,
            }
        }
        for nid, entry in self._directory.items():
            if nid == self.node_id or entry.get("role") != ROLE_FIELD_EGG:
                continue
            uwb_id = entry.get("uwb_id")
            if uwb_id is None:
                continue
            try:
                uwb_id = int(uwb_id)
            except Exception:
                continue
            if not (0 <= uwb_id <= 7):
                continue
            self._members[nid] = {
                "node_id":   nid,
                "name":      entry.get("name", "egg_{}".format(nid)),
                "uwb_id":    uwb_id,
                "last_seen": now,
            }
            self.logger.item("Peer", "{} uwb {}".format(nid, uwb_id))

    # ------------------------------------------------------------------ #
    # Coordinator phase                                                    #
    # ------------------------------------------------------------------ #

    def _phase_coordinator(self, now, members):
        """
        Full round-robin ranging for a complete pairwise distance matrix.

        1. Broadcast LOCALISE_START so followers know the member list.
        2. Coordinator measures as tag, returns to anchor.
        3. Each follower is sent LOCALISE_TURN; coordinator polls for result.
        4. Solve MDS, broadcast positions, switch to permanent tag.
        """
        self.state = self.STATE_COORDINATOR
        self._dist_matrix    = {}
        self._results        = {}
        self._expected_total = len(members)
        self.logger.event("LOCALISATION COORDINATOR", [
            ("Members", self._member_ids(members)),
        ])

        # Announce member list so followers can prepare for their ranging turn.
        self._send_start(members)
        utime.sleep_ms(500)

        # Coordinator's own ranging turn — returns to anchor for follower turns.
        distances = self.ranger.measure(members, self.node_id, stay_as_tag=False)
        self._record_result(self.node_id, distances, utime.ticks_ms())
        gc.collect()

        # Each follower takes a turn as tag.
        for member in [m for m in members if m["node_id"] != self.node_id]:
            follower_id = member["node_id"]
            self._send_turn(members, follower_id)
            deadline = utime.ticks_add(utime.ticks_ms(), self.turn_ms)
            while utime.ticks_diff(utime.ticks_ms(), deadline) < 0:
                self._poll_radio(utime.ticks_ms())
                if follower_id in self._results:
                    break
                utime.sleep_ms(50)
            gc.collect()

        self._solve_and_broadcast(members)

    def _solve_and_broadcast(self, members):
        node_ids = [m["node_id"] for m in members]
        try:
            solved = solve_from_distance_matrix(node_ids, self._dist_matrix)
        except Exception as exc:
            self.logger.event("LOCALISATION SOLVE ERROR", [("Error", exc)])
            self._finish(utime.ticks_ms(), "solve failed")
            return

        if not solved:
            self._finish(utime.ticks_ms(), "no positions")
            return

        for k in list(self._positions):
            del self._positions[k]
        for nid, coords in solved.items():
            self._positions[nid] = (
                round(coords[0], 4), round(coords[1], 4), round(coords[2], 4)
            )

        self._set_self_pos(self._positions.get(self.node_id))
        self._broadcast_positions()
        self._finish(utime.ticks_ms(), "coordinator solved")

    # ------------------------------------------------------------------ #
    # Follower phase                                                       #
    # ------------------------------------------------------------------ #

    def _phase_follower_turn(self, members, coordinator, now):
        """Measure distances as tag then send LOCALISE_RESULT to coordinator."""
        distances = self.ranger.measure(members, self.node_id)
        packet = make_packet(
            LOCALISE_RESULT,
            self.node_id, coordinator,
            self._next_seq(), ttl=1,
            payload={"coordinator": coordinator, "d": distances},
        )
        self.logger.packet("TX", packet, [("Distances", distances)], compact=True)
        self._send_packet(packet)
        if self.state != self.STATE_STEADY:
            self.state = self.STATE_WAITING
            self._deadline = utime.ticks_add(now, max(len(members), 2) * self.turn_ms)

    def _phase_waiting(self, now):
        if self._deadline is None or utime.ticks_diff(now, self._deadline) < 0:
            return
        if self._positions.get(self.node_id) is not None:
            self._finish(now, "map timeout with self position")
        else:
            self.logger.event("LOCALISATION RETRY", [("Reason", "timeout waiting for map")])
            self.start(now, reason="retry")

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _record_result(self, source_id, distances, now):
        self._results[source_id] = True
        if source_id in self._members:
            self._members[source_id]["last_seen"] = now
        clean = {}
        for key, dist in distances.items():
            try:
                target_id = int(key)
                d = float(dist)
            except Exception:
                continue
            if target_id == source_id or d <= 0:
                continue
            self._dist_matrix[(source_id, target_id)] = d
            self._neighbours.update_range(target_id, d)
            clean[target_id] = d
        self.logger.item("Localise result", "{} targets".format(len(clean)))
        # Emit structured distance line so a PC can run MDS independently.
        pairs = " ".join("{}:{:.4f}".format(t, d) for t, d in clean.items())
        self.logger.event("RANGE_DATA", [("src", source_id), ("targets", pairs)])

    def _broadcast_positions(self):
        total = len(self._positions)
        for nid in sorted(self._positions.keys()):
            x, y, z = self._positions[nid]
            packet = make_packet(
                LOCALISE_POSITION,
                self.node_id, BROADCAST,
                self._next_seq(), ttl=1,
                payload={
                    "coordinator": self.node_id,
                    "node_id": nid,
                    "x": x, "y": y, "z": z,
                    "total": total,
                },
            )
            self.logger.packet("TX", packet, [("Node", nid)], compact=True)
            self._send_packet(packet)
            utime.sleep_ms(80)

    def _finish(self, now, reason):
        self.state       = self.STATE_STEADY
        self.is_complete = True
        self._deadline   = None

        is_tag = (self._coordinator == self.node_id and reason not in ("solo node",))
        if is_tag:
            self.ranger.set_tag()

        items = [("Reason", reason)]
        if self._coordinator is not None:
            items.append(("Coordinator", self._coordinator))
        items.append(("UWB role", "tag" if is_tag else "anchor"))
        pos = self._positions.get(self.node_id)
        if pos is not None:
            items.append(("Position", "{:.2f}, {:.2f}".format(pos[0], pos[1])))
        self.logger.event("LOCALISATION DONE", items)
        self._on_complete(now)

    def _send_start(self, members):
        """Coordinator broadcasts LOCALISE_START with definitive member list."""
        packet = make_packet(
            LOCALISE_START,
            self.node_id, BROADCAST,
            self._next_seq(), ttl=1,
            payload={
                "coordinator": self.node_id,
                "members": [
                    {"node_id": m["node_id"], "uwb_id": m["uwb_id"], "name": m.get("name", "")}
                    for m in members
                ],
            },
        )
        self.logger.packet("TX", packet, compact=True)
        self._send_packet(packet)

    def _send_turn(self, members, follower_id):
        packet = make_packet(
            LOCALISE_TURN,
            self.node_id, follower_id,
            self._next_seq(), ttl=1,
            payload={
                "coordinator": self.node_id,
                "members": [{"node_id": m["node_id"], "uwb_id": m["uwb_id"]} for m in members],
            },
        )
        self.logger.packet("TX", packet, [("To", follower_id)], compact=True)
        self._send_packet(packet)

    def _send_discovery(self, now):
        """One-shot signal so peers in STEADY know to restart with us."""
        packet = make_packet(
            LOCALISE_DISCOVERY,
            self.node_id, BROADCAST,
            self._next_seq(), ttl=1,
            payload={"name": self.node_name, "uwb_id": self.ranger.uwb_id},
        )
        self.logger.packet("TX", packet, compact=True)
        self._send_packet(packet)

    def _member_list(self):
        members = [
            {"node_id": m["node_id"], "name": m.get("name", "egg_{}".format(m["node_id"])), "uwb_id": m["uwb_id"]}
            for m in self._members.values()
            if m.get("uwb_id") is not None and 0 <= m["uwb_id"] <= 7
        ]
        members.sort(key=lambda x: x["node_id"])

        if len(members) <= self.max_members:
            return members

        selected = members[:self.max_members]
        if not any(m["node_id"] == self.node_id for m in selected):
            selected[-1] = {"node_id": self.node_id, "name": self.node_name, "uwb_id": self.ranger.uwb_id}
            selected.sort(key=lambda x: x["node_id"])
        self.logger.event("LOCALISATION LIMIT", [
            ("Using", self._member_ids(selected)),
            ("Seen",  self._member_ids(members)),
        ])
        return selected

    def _member_ids(self, members):
        return ",".join(str(m["node_id"]) for m in members)

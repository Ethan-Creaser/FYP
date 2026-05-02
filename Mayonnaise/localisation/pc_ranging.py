import gc
import utime

from localisation.ranging import UWBRanger

ROLE_FIELD_EGG = "field_egg"

# How long to wait between full ranging rounds (ms).
# Should be long enough that all anchors are settled before next tag turn.
_ROUND_INTERVAL_MS = 10000


class PCRangingController:
    """
    Simplified localisation controller for PC-centralised mode.

    Instead of running coordinator election and on-device MDS, this
    controller just:
      1. Configures UWB as anchor during idle periods.
      2. On each round: switches to tag, ranges all known peers, switches back.
      3. Emits a RANGE_DATA log line with the raw distances.
      4. The PC reads these lines via BLE and runs MDS itself.

    No coordinator election.  No state machine beyond "ranging" / "idle".
    A failing node simply stops emitting — the PC drops stale entries and
    re-solves with whoever remains.

    To activate: in node.py replace LocalisationController with this class.
    The constructor signature is identical so it's a drop-in swap.
    """

    STATE_IDLE    = "idle"
    STATE_RANGING = "ranging"

    # Stub is_complete / state so EggNode code that checks these still works.
    is_complete = False

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
        self.state     = self.STATE_IDLE

        self.ranger = UWBRanger(
            uwb       = uwb,
            logger    = logger,
            uwb_id    = uwb_id,
            channel   = uwb_channel,
            rate      = uwb_rate,
            frames    = config.get("localisation_frames",     20),
            settle_ms = config.get("localisation_settle_ms", 3000),
        )

        self._poll_radio   = poll_radio_fn
        self._neighbours   = neighbours
        self._directory    = node_directory
        self._set_self_pos = set_self_pos_fn

        self._round_ms  = config.get("pc_ranging_round_ms", _ROUND_INTERVAL_MS)
        self._next_due  = None

    # ------------------------------------------------------------------ #
    # Public interface (same as LocalisationController)                   #
    # ------------------------------------------------------------------ #

    def start(self, now, reason="boot"):
        self.logger.event("PC RANGING START", [
            ("Reason", reason), ("Node", self.node_id),
        ])
        self.ranger.set_anchor(cold=(reason == "boot"))
        # Stagger first round by node_id seconds so nodes don't all switch to
        # tag simultaneously on boot and starve each other of anchors.
        stagger_ms = self.node_id * self._round_ms
        self._next_due = utime.ticks_add(utime.ticks_ms(), stagger_ms)
        self.state = self.STATE_IDLE

    def advance(self, now):
        if self._next_due is None:
            return
        if utime.ticks_diff(now, self._next_due) < 0:
            return
        self._do_ranging_round(now)
        self._next_due = utime.ticks_add(utime.ticks_ms(), self._round_ms)

    # ------------------------------------------------------------------ #
    # Packet handlers — stubs so EggNode call sites don't break           #
    # ------------------------------------------------------------------ #

    def handle_discovery(self, packet, now):
        pass

    def handle_start(self, packet, now):
        pass

    def handle_turn(self, packet, now):
        pass

    def handle_result(self, packet, now):
        pass

    def handle_position(self, packet, now):
        # Accept positions broadcast by the PC (or a coordinator on another node)
        # and store them locally so the mesh can relay them onward.
        payload = packet.get("p", {})
        node_id = payload.get("node_id")
        if node_id is None:
            return
        try:
            pos = (
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                float(payload.get("z", 0.0)),
            )
        except Exception:
            return
        if node_id == self.node_id:
            self._set_self_pos(pos)

    # ------------------------------------------------------------------ #
    # Ranging round                                                        #
    # ------------------------------------------------------------------ #

    def _do_ranging_round(self, now):
        members = self._build_members()
        if len(members) <= 1:
            self.logger.event("PC RANGING SKIP", [("Reason", "no peers in directory")])
            return

        self.state = self.STATE_RANGING
        self.logger.event("PC RANGING ROUND", [
            ("Peers", ",".join(str(m["node_id"]) for m in members
                               if m["node_id"] != self.node_id)),
        ])

        distances = self.ranger.measure(members, self.node_id, stay_as_tag=False)
        gc.collect()

        if distances:
            for tgt_str, d in distances.items():
                try:
                    self._neighbours.update_range(int(tgt_str), d)
                except Exception:
                    pass
            # Emit RANGE_DATA — parsed by PC_Scripts/pc_localisation.py
            pairs = " ".join("{}:{}".format(t, d) for t, d in distances.items())
            self.logger.event("RANGE_DATA", [("src", self.node_id), ("targets", pairs)])
        else:
            self.logger.event("PC RANGING ROUND", [("Result", "no distances")])

        self.state = self.STATE_IDLE

    def _build_members(self):
        members = [{
            "node_id": self.node_id,
            "uwb_id":  self.ranger.uwb_id,
            "name":    self.node_name,
        }]
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
            members.append({
                "node_id": nid,
                "uwb_id":  uwb_id,
                "name":    entry.get("name", "egg_{}".format(nid)),
            })
        return members

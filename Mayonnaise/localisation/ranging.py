import utime


class UWBRanger:
    """
    Owns all UWB hardware interaction for localisation.

    Responsible for:
      - role switching (anchor <-> tag) via configure / configure_warm
      - measuring distances to a set of member nodes
    """

    ROLE_ANCHOR = 1
    ROLE_TAG    = 0

    def __init__(self, uwb, logger, uwb_id, channel, rate, frames, settle_ms):
        self.uwb       = uwb
        self.logger    = logger
        self.uwb_id    = uwb_id
        self.channel   = channel
        self.rate      = rate
        self.frames    = frames
        self.settle_ms = settle_ms
        self.ready     = False   # True once first configure() has succeeded

    # ------------------------------------------------------------------ #
    # Role control                                                         #
    # ------------------------------------------------------------------ #

    def set_mode(self, role, cold=False):
        """
        Configure UWB as anchor (ROLE_ANCHOR=1) or tag (ROLE_TAG=0).

        Uses cold configure on first call or when cold=True,
        warm configure for subsequent switches.
        Returns True on success.
        """
        if self.uwb is None:
            return False
        try:
            fn = self.uwb.configure if (cold or not self.ready) else self.uwb.configure_warm
            fn(self.uwb_id, role=role, channel=self.channel, rate=self.rate)
            self.ready = True
            return True
        except Exception as exc:
            self.logger.event("UWB CONFIG ERROR", [("Error", exc)])
            return False

    def set_anchor(self, cold=False):
        return self.set_mode(self.ROLE_ANCHOR, cold=cold)

    def set_tag(self):
        return self.set_mode(self.ROLE_TAG, cold=False)

    # ------------------------------------------------------------------ #
    # Distance measurement                                                 #
    # ------------------------------------------------------------------ #

    def measure(self, members, self_node_id, stay_as_tag=False):
        """
        Switch to tag, wait for anchors to detect us, scan UWB frames.

        stay_as_tag:  if True, remain in tag mode after measuring (permanent tag).
                      if False (default), restore anchor mode when done.

        members:      list of dicts with "node_id" and "uwb_id" keys
        self_node_id: skip self when building the result dict

        Returns dict of {str(node_id): distance_m} for each reachable peer.
        UWB slot index == peer's uwb_id, so we look up raw[peer_uwb_id]
        directly rather than using sorted-order heuristics.
        """
        if self.uwb is None:
            return {}
        try:
            self.set_tag()
            utime.sleep_ms(self.settle_ms)   # anchors need time to detect new tag
            self.uwb.flush()
            raw = self.uwb.scan(frames=self.frames)
            self.logger.event("UWB SCAN", [("Raw", raw)])
        except Exception as exc:
            self.logger.event("LOCALISATION RANGE ERROR", [("Error", exc)])
            raw = {}
        finally:
            if not stay_as_tag:
                self.set_anchor()

        distances = {}
        for member in members:
            other_id  = member.get("node_id")
            other_uwb = member.get("uwb_id")
            if other_id == self_node_id or other_uwb is None:
                continue
            if not (0 <= other_uwb <= 7):
                continue
            d = raw.get(other_uwb)
            if d is not None and d > 0:
                distances[str(other_id)] = round(float(d), 4)
        return distances

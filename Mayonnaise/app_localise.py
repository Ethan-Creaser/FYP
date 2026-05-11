"""Localisation application layer — mesh/UWB bridge.

This module is the join point between the mesh routing layer and the UWB
ranging / trilateration code.  The mesh routes bytes; this module interprets
them.

Deleting this file leaves the mesh node fully functional — no imports or
behaviour in node.py depend on this module being present.

Teammate usage
--------------
Subclass LocaliseApp, override on_rx(), then attach it to the node:

    from app_localise import LocaliseApp, LOC_RANGE_REQ, LOC_RANGE_RESP

    class MyLoc(LocaliseApp):
        def on_rx(self, src_mesh_id, subtype, payload):
            if subtype == LOC_RANGE_REQ:
                distance = self.uwb.measure(src_mesh_id)
                self.send(src_mesh_id, LOC_RANGE_RESP, encode_float(distance))
            elif subtype == LOC_RANGE_RESP:
                trilat.add_measurement(src_mesh_id, decode_float(payload))

    loc = MyLoc(node)   # registers itself; mesh will call on_rx / on_ctrl / tick

To send a ranging request to egg 5:
    loc.send(dst_mesh_id=5, subtype=LOC_RANGE_REQ)

UWB role assignment
-------------------
The gateway egg (the one connected to the PC over BLE) receives UWB config
commands from the PC via _bt_rx in main.py.  Those commands are forwarded
over the mesh as APP_CTRL packets.  on_ctrl() on the target egg handles them,
defers the actual UWB reconfiguration into _uwb_pending, and tick() executes
it outside the radio callback stack.
"""

import constants

# ── APP_LOCALISE subtype constants ────────────────────────────────────────────
LOC_RANGE_REQ  = 1   # request a UWB range measurement
LOC_RANGE_RESP = 2   # reply carrying the measured distance
LOC_POSITION   = 3   # broadcast computed position estimate

_UWB_SCAN_FRAMES = 20


class LocaliseApp:
    def __init__(self, node):
        self.node = node
        node.localise_app = self   # registers with node so mesh calls on_rx/on_ctrl/tick

        self.uwb          = None   # set by main.py after construction if use_uwb
        self._uwb_pending = None   # (uwb_id, role, src) or None — executed by tick()

    # ── Mesh send helper ──────────────────────────────────────────────────────

    def send(self, dst_mesh_id, subtype, payload=b""):
        """Send a localisation payload through the mesh."""
        self.node.send_data(
            dst=dst_mesh_id,
            app_id=constants.APP_LOCALISE,
            subtype=subtype,
            data=payload,
        )

    # ── Mesh receive callbacks (called by node) ───────────────────────────────

    def on_rx(self, src_mesh_id, subtype, payload):
        """Called by the mesh when an APP_LOCALISE packet arrives for this node.

        Teammate: override this with your ranging/trilateration logic.
        """
        print("[localise] RX from={} subtype={} len={}".format(
            src_mesh_id, subtype, len(payload)))

    def on_ctrl(self, src_mesh_id, subtype, body):
        """Called by the mesh when an APP_CTRL packet arrives for this node.

        Handles UWB role-assignment commands sent by the PC via the gateway egg.
        """
        if subtype == constants.CTRL_UWB_CONFIG and len(body) >= 2:
            uwb_id = body[0]
            role   = body[1]
            print("[localise] UWB config: uwb_id={} role={}".format(uwb_id, role))
            if self.uwb is not None:
                # Defer — configure_warm blocks for ~5 s; don't call it inside
                # the radio receive callback.
                self._uwb_pending = (uwb_id, role, src_mesh_id)
            else:
                print("[localise] UWB not attached")

        elif subtype == constants.CTRL_UWB_RESTORE:
            print("[localise] UWB restore command received")
            if self.uwb is not None:
                # uwb_id=None signals tick() to restore from the stored default
                self._uwb_pending = (None, 1, src_mesh_id)
            else:
                print("[localise] UWB not attached")

        elif subtype == constants.CTRL_UWB_SCAN_RESULT and len(body) >= 2:
            uwb_id = body[0]
            role   = body[1]
            i = 2
            while i + 2 < len(body):
                slot    = body[i]
                dist_mm = (body[i + 1] << 8) | body[i + 2]
                print("UWB_RESULT node={} uwb_id={} role={} slot={} dist={:.4f}".format(
                    src_mesh_id, uwb_id, role, slot, dist_mm / 1000.0))
                i += 3

    # ── Periodic tick (called by node.tick()) ─────────────────────────────────

    def tick(self):
        """Execute any deferred UWB work.

        Called by node.tick() every ~5 s.  UWB configure_warm() blocks for
        several seconds and must not run inside the radio IRQ/callback stack.
        """
        if self._uwb_pending is None:
            return

        uwb_id, role, src = self._uwb_pending
        self._uwb_pending = None

        if uwb_id is None:
            # Restore command: revert to the default uwb_id stored at boot
            default_id = getattr(self, "uwb_default_id", None)
            if default_id is not None:
                print("[localise] UWB restoring to uwb_id={} role=1 (anchor)".format(
                    default_id))
                try:
                    self.uwb.configure_warm(default_id, 1)
                    print("[localise] UWB restored ok")
                except Exception as e:
                    print("[localise] UWB restore failed:", e)
            else:
                print("[localise] UWB restore: no default id stored")
        else:
            # Config command: set new uwb_id/role, then scan and report back
            try:
                self.uwb.configure_warm(uwb_id, role)
                print("[localise] UWB reconfigured ok")
                self._scan_and_send_uwb(uwb_id, role, src)
            except Exception as e:
                print("[localise] UWB reconfigure failed:", e)

    # ── UWB scan ──────────────────────────────────────────────────────────────

    def _scan_and_send_uwb(self, uwb_id, role, requester_id):
        print("[localise] UWB scan ({} frames)...".format(_UWB_SCAN_FRAMES))
        self.uwb.flush()
        raw = self.uwb.scan(frames=_UWB_SCAN_FRAMES)

        if not raw:
            print("[localise] UWB scan: no data")
            return

        payload = bytearray([uwb_id, role])
        for slot, dist in sorted(raw.items()):
            print("[localise]   slot {} -> {:.4f} m".format(slot, dist))
            dist_mm = min(int(dist * 1000), 0xFFFF)
            payload.append(slot & 0xFF)
            payload.append((dist_mm >> 8) & 0xFF)
            payload.append(dist_mm & 0xFF)

        dst = requester_id if requester_id is not None else constants.GROUND_STATION_ID
        self.node.send_data(dst, constants.APP_CTRL, constants.CTRL_UWB_SCAN_RESULT,
                            bytes(payload))
        print("[localise] UWB scan sent to node {}".format(dst))

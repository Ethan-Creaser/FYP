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

        self.uwb           = None   # set by main.py after construction if use_uwb
        self._uwb_pending  = None   # (uwb_id, role, src) or None — executed by tick()
        self._last_scan_dst = None  # destination of the most recent scan send

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
        if subtype == constants.CTRL_UWB_SCAN_RESULT:
            uwb_id = body[0] if len(body) > 0 else 0
            role   = body[1] if len(body) > 1 else 0
            n_slots = (len(body) - 2) // 3 if len(body) >= 2 else 0
            print("[localise] SCAN RESULT from={} uwb_id={} role={} slots={}".format(
                src_mesh_id, uwb_id, role, n_slots))
            for i in range(n_slots):
                off = 2 + i * 3
                slot    = body[off]
                dist_mm = (body[off + 1] << 8) | body[off + 2]
                dist    = dist_mm / 1000.0
                print("[localise]   slot {} -> {:.4f} m".format(slot, dist))
                print("UWB_RESULT node={} uwb_id={} role={} slot={} dist={:.4f}".format(
                    src_mesh_id, uwb_id, role, slot, dist))
            return

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

        elif subtype == constants.CTRL_IDENTITY_WRITE:
            if len(body) < 2:
                print("[localise] IDENTITY_WRITE: payload too short")
                return
            uwb_id    = body[0]
            count     = body[1]
            neighbors = list(body[2:2 + count]) if count > 0 else []
            node_id   = self.node.node_id
            print("[localise] IDENTITY_WRITE: node_id={} uwb_id={} neighbors={}".format(
                node_id, uwb_id, neighbors))
            try:
                from identity import write_identity, read_identity
                existing = read_identity()
                cur_beacon = existing[3] if existing else True
                write_identity(node_id, uwb_id, allowed_neighbors=neighbors or None,
                               beacon_enabled=cur_beacon)
                self.node.neighbours.allowlist = set(neighbors) if neighbors else None
                # Machine-parseable confirmation (visible on this egg's own BLE/serial)
                nb_str = ",".join(str(n) for n in neighbors)
                print("IDENTITY_OK node_id={} uwb_id={} neighbors={}".format(
                    node_id, uwb_id, nb_str))
                # Send ACK back through the mesh so the gateway can relay it to the PC
                ack = bytearray([node_id & 0xFF, uwb_id & 0xFF, len(neighbors) & 0xFF])
                ack.extend(n & 0xFF for n in neighbors)
                self.node.send_data(src_mesh_id, constants.APP_CTRL,
                                    constants.CTRL_IDENTITY_ACK, bytes(ack))
            except Exception as e:
                print("[localise] IDENTITY_FAIL reason={}".format(e))

        elif subtype == constants.CTRL_IDENTITY_ACK:
            # Received by the gateway egg — forward confirmation to the PC via BLE print
            if len(body) < 3:
                print("[localise] IDENTITY_ACK: payload too short")
                return
            ack_node_id   = body[0]
            ack_uwb_id    = body[1]
            ack_count     = body[2]
            ack_neighbors = list(body[3:3 + ack_count])
            nb_str = ",".join(str(n) for n in ack_neighbors)
            # Same IDENTITY_OK format as the direct path — PC parser handles both identically
            print("IDENTITY_OK node_id={} uwb_id={} neighbors={}".format(
                ack_node_id, ack_uwb_id, nb_str))

        elif subtype == constants.CTRL_BEACON:
            if len(body) < 1:
                print("[localise] CTRL_BEACON: payload too short")
                return
            enabled = bool(body[0])
            node_id = self.node.node_id
            print("[localise] BEACON {}: node_id={}".format(
                "ENABLE" if enabled else "DISABLE", node_id))
            try:
                from identity import write_identity, read_identity
                existing = read_identity()
                if existing:
                    write_identity(existing[0], existing[1],
                                   allowed_neighbors=existing[2],
                                   beacon_enabled=enabled)
                self.node.beacon_enabled = enabled
                print("BEACON_OK node_id={} enabled={}".format(node_id, int(enabled)))
                ack = bytes([node_id & 0xFF, 1 if enabled else 0])
                self.node.send_data(src_mesh_id, constants.APP_CTRL,
                                    constants.CTRL_IDENTITY_ACK, ack)
            except Exception as e:
                print("[localise] BEACON_FAIL reason={}".format(e))

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

    def on_route_fail(self, target_id, dropped_pkts):
        """Called by node when RREQ exhausted and scan result packets are dropped."""
        print("[localise] WARNING: route to node {} failed — {} scan result packet(s) lost".format(
            target_id, len(dropped_pkts)))

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
        self._last_scan_dst = dst
        next_hop = self.node.routes.get_next_hop(dst)
        print("[localise] sending scan to node {} via next_hop={} ({} slot readings)".format(
            dst, next_hop, len(raw)))
        self.node.send_data(dst, constants.APP_CTRL, constants.CTRL_UWB_SCAN_RESULT,
                            bytes(payload))
        print("[localise] UWB scan queued seq={} dst={} next_hop={}".format(
            self.node._seq - 1, dst, next_hop))

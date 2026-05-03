"""Mesh node core: state machine, forwarding, and route discovery.

Designed to run on both MicroPython (ESP32-S3) and CPython (PC simulation).
No typing imports. No dataclasses. No __future__ annotations.
"""

import time
import json

import constants
import packets
from neighbour_table import NeighbourTable
from route_table import RouteTable

_MAX_PENDING_PER_DST = 3   # max DATA packets buffered per destination while waiting for a route


class Node:
    def __init__(self, node_id, allowlist=None):
        self.node_id    = node_id
        self.neighbours = NeighbourTable(allowlist=allowlist)
        self.routes     = RouteTable()
        self.network    = None        # set by SimNetwork.register_node
        self.radio      = None        # set by attach_hardware_from_config
        self.uwb        = None        # set by main.py if use_uwb
        self.start_time = time.time()

        self._seq       = 1
        self._last_tx_time = 0        # updated on every send; used for beacon suppression

        # Duplicate suppression: (src, seq) -> timestamp of first receipt
        self._seen = {}

        # Hop-by-hop ACK relay table: (orig_src, orig_seq) -> prev_hop node_id
        # Entries are for packets we forwarded (not originated).
        self.pending_forwards = {}

        # Packets we originated, waiting for the first-hop ACK: (src, seq) -> sent_time
        self.outstanding = {}

        # DATA packets buffered because no route exists yet: dst -> [pkt, ...]
        self._pending_data = {}

        # In-flight RREQ: target_id -> origin_seq we used for that RREQ
        self._rreq_pending = {}

    # ── Sequence numbers ──────────────────────────────────────────────────────

    def next_seq(self):
        s = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        if self._seq == 0:
            self._seq = 1
        return s

    # ── Sending ───────────────────────────────────────────────────────────────

    def send_packet(self, pkt):
        """Transmit a packet.

        Stamps sender_id = self.node_id before serialising so the receiver
        always knows the physical last-hop sender (needed for hop-by-hop ACKs
        in hardware where the radio driver has no node-ID awareness).

        Always broadcasts — this matches LoRa's physical behaviour and ensures
        all nearby nodes refresh their neighbour-table entries on every TX.
        """
        pkt.sender_id = self.node_id
        self._last_tx_time = time.time()
        b = pkt.to_bytes()
        if self.network is not None:
            # Sim: deliver to all topology neighbours (broadcast, like real LoRa)
            self.network.deliver(b, from_id=self.node_id)
            return
        if self.radio is not None:
            self.radio.send(b)
            return
        # Fallback for tests with neither sim nor hardware
        print("[{}] TX kind={} src={} dst={} seq={} ttl={}".format(
            self.node_id, pkt.kind, pkt.src, pkt.dst, pkt.seq, pkt.ttl))

    def send_beacon(self):
        seq = self.next_seq()
        h   = self._compute_hops_to_ground()
        pkt = packets.make_beacon(src=self.node_id, seq=seq, hops_to_ground=h)
        print("[{}] BEACON seq={} hops_to_ground={}".format(self.node_id, seq, h))
        self.send_packet(pkt)

    def send_data(self, dst, app_id, subtype, data=b"", ttl=constants.MAX_TTL):
        """Send an application DATA packet.

        If a cached route exists the packet goes immediately.
        Otherwise it is buffered and a RREQ flood is triggered.
        """
        seq = self.next_seq()
        pkt = packets.make_data(
            src=self.node_id, dst=dst, seq=seq, ttl=ttl,
            app_id=app_id, subtype=subtype, data=data
        )
        self.outstanding[(self.node_id, seq)] = time.time()

        next_hop = self.routes.get_next_hop(dst)
        print("[{}] SEND dst={} seq={} next_hop={}".format(self.node_id, dst, seq, next_hop))

        if next_hop is not None:
            self.send_packet(pkt)
        else:
            buf = self._pending_data.setdefault(dst, [])
            if len(buf) < _MAX_PENDING_PER_DST:
                buf.append(pkt)
            self._flood_rreq(dst)

    def _flood_rreq(self, target_id):
        """Send a RREQ for target_id unless one is already in flight."""
        if target_id in self._rreq_pending:
            return
        seq = self.next_seq()
        self._rreq_pending[target_id] = seq
        pkt = packets.make_rreq(src=self.node_id, seq=seq, target_id=target_id)
        print("[{}] RREQ flood target={} seq={}".format(self.node_id, target_id, seq))
        self.send_packet(pkt)

    # ── Receiving ─────────────────────────────────────────────────────────────

    def receive_raw(self, data, rssi=None, snr=None):
        """Entry point from the radio (hardware or sim).

        from_id is derived from pkt.sender_id — the node that physically
        transmitted this packet — rather than passed in as a parameter.
        This works in both hardware (where the radio driver has no node-ID
        knowledge) and the simulator.
        """
        try:
            pkt = packets.Packet.from_bytes(data)
        except Exception as e:
            print("[{}] bad packet: {}".format(self.node_id, e))
            return

        from_id = pkt.sender_id

        # Every received packet refreshes the sender's neighbour entry.
        self.neighbours.update(from_id, rssi=rssi, snr=snr)

        # Update OLED/display if attached
        disp = getattr(self, "display", None)
        if disp:
            try:
                disp.update_on_rx(rssi=rssi, snr=snr, from_id=from_id, src=pkt.src)
            except Exception:
                pass

        self.handle_packet(pkt, from_id)

    def _seen_check(self, pkt):
        """Return True if (src, seq) was seen recently (duplicate). Record if new."""
        key = (pkt.src, pkt.seq)
        if key in self._seen:
            return True
        self._seen[key] = time.time()
        # Prune oldest half when cache gets large (MicroPython-safe: no heapq)
        if len(self._seen) > 256:
            items = sorted(self._seen.items(), key=lambda x: x[1])
            for k, _ in items[:len(items) // 2]:
                del self._seen[k]
        return False

    def handle_packet(self, pkt, from_id):
        # Drop our own packets when they echo back via the broadcast medium
        if pkt.src == self.node_id:
            return

        if pkt.kind == constants.KIND_BEACON:
            self._handle_beacon(pkt)
            return

        if pkt.kind == constants.KIND_ACK:
            # ACK is addressed to a specific hop — ignore if not for us.
            if pkt.dst != self.node_id:
                return
            self._handle_ack(pkt, from_id)
            return

        if pkt.kind == constants.KIND_DATA:
            if self._seen_check(pkt):
                return
            self._handle_data(pkt, from_id)
            return

        if pkt.kind == constants.KIND_BCAST:
            if self._seen_check(pkt):
                return
            self._handle_bcast(pkt, from_id)
            return

    # ── BEACON ────────────────────────────────────────────────────────────────

    def _handle_beacon(self, pkt):
        h = int(pkt.payload[0]) if pkt.payload else 255
        self.neighbours.update(pkt.src, hops_to_ground=(None if h == 255 else h))
        print("[{}] BEACON from={} hops_to_ground={}".format(self.node_id, pkt.src, h))

    def _compute_hops_to_ground(self):
        if self.node_id == constants.GROUND_STATION_ID:
            return 0
        best = None
        for e in self.neighbours.get_alive():
            if e.hops_to_ground is not None:
                if best is None or e.hops_to_ground < best:
                    best = e.hops_to_ground
        if best is None:
            return 255
        return min(best + 1, 254)

    # ── DATA ──────────────────────────────────────────────────────────────────

    def _handle_data(self, pkt, from_id):
        if pkt.dst == self.node_id:
            # Send hop-by-hop ACK back to the physical sender
            ack = packets.make_ack(
                src=self.node_id, dst=from_id,
                orig_src=pkt.src, orig_seq=pkt.seq
            )
            self.send_packet(ack)

            # Parse app layer
            try:
                app_id, subtype, body = packets.parse_app_payload(pkt.payload)
            except Exception:
                app_id, subtype, body = None, None, pkt.payload

            if app_id == constants.APP_ROUTING:
                self._handle_routing_data(subtype, body, from_id)
            else:
                self._deliver_to_app(pkt, app_id, subtype, body)
            return

        # Not for me — forward
        if pkt.ttl <= 1:
            return
        pkt.ttl -= 1

        # Record prev_hop so we can relay the ACK back when it arrives
        self.pending_forwards[(pkt.src, pkt.seq)] = from_id

        # If a RREP is passing through, opportunistically cache the forward route
        if len(pkt.payload) >= 6:
            try:
                app_id = pkt.payload[0]
                sub    = pkt.payload[1]
                if app_id == constants.APP_ROUTING and sub == constants.ROUTING_RREP:
                    target_id, _, hop_count = packets.parse_rrep(pkt.payload[2:])
                    self.routes.set_route(target_id, from_id, hop_count)
            except Exception:
                pass

        print("[{}] FWD DATA src={} dst={} seq={} ttl={}".format(
            self.node_id, pkt.src, pkt.dst, pkt.seq, pkt.ttl))
        self.send_packet(pkt)

    def _deliver_to_app(self, pkt, app_id, subtype, body):
        print("[{}] DELIVER src={} app={} sub={} len={}".format(
            self.node_id, pkt.src, app_id, subtype, len(body)))
        if app_id == constants.APP_CTRL:
            self._handle_app(subtype, body)

    def _handle_app(self, subtype, body):
        if subtype == constants.CTRL_UWB_CONFIG and len(body) >= 2:
            uwb_id = body[0]
            role   = body[1]
            print("[{}] UWB config: uwb_id={} role={}".format(self.node_id, uwb_id, role))
            if self.uwb is not None:
                try:
                    self.uwb.configure_warm(uwb_id, role)
                    print("[{}] UWB reconfigured ok".format(self.node_id))
                except Exception as e:
                    print("[{}] UWB reconfigure failed: {}".format(self.node_id, e))
            else:
                print("[{}] UWB not attached".format(self.node_id))

    # ── BCAST ─────────────────────────────────────────────────────────────────

    def _handle_bcast(self, pkt, from_id):
        try:
            app_id, subtype, body = packets.parse_app_payload(pkt.payload)
        except Exception:
            app_id, subtype, body = None, None, pkt.payload

        if app_id == constants.APP_ROUTING:
            self._handle_routing_bcast(subtype, body, from_id, pkt)
        else:
            self._deliver_bcast_to_app(pkt, app_id, subtype, body)

        # Rebroadcast with decremented TTL
        if pkt.ttl > 1:
            pkt.ttl -= 1
            self.send_packet(pkt)

    def _deliver_bcast_to_app(self, pkt, app_id, subtype, body):
        print("[{}] BCAST src={} app={} sub={} len={}".format(
            self.node_id, pkt.src, app_id, subtype, len(body)))

    # ── Routing ───────────────────────────────────────────────────────────────

    def _handle_routing_bcast(self, subtype, body, from_id, pkt):
        if subtype == constants.ROUTING_RREQ:
            try:
                target_id, hop_count = packets.parse_rreq(body)
            except Exception:
                return

            origin_id  = pkt.src
            origin_seq = pkt.seq

            # Cache the reverse path to the origin so RREP can travel back
            if from_id != origin_id:
                self.routes.set_route(origin_id, from_id, hop_count + 1)

            print("[{}] RREQ origin={} target={} hops={}".format(
                self.node_id, origin_id, target_id, hop_count))

            if target_id == self.node_id:
                self._send_rrep(origin_id, origin_seq, hop_count + 1)
            else:
                # Update hop_count in payload before the BCAST handler rebroadcasts
                if pkt.ttl > 1:
                    new_body = bytes([target_id, (hop_count + 1) & 0xFF])
                    pkt.payload = bytes([constants.APP_ROUTING, constants.ROUTING_RREQ]) + new_body

        elif subtype == constants.ROUTING_RECOVERY:
            lost_id = body[0] if body else None
            if lost_id is not None:
                self.routes.invalidate_next_hop(lost_id)
                print("[{}] RECOVERY lost={}".format(self.node_id, lost_id))

    def _send_rrep(self, origin_id, origin_seq, hop_count):
        seq = self.next_seq()
        pkt = packets.make_rrep(
            src=self.node_id,
            dst=origin_id,
            seq=seq,
            target_id=self.node_id,
            origin_seq=origin_seq,
            hop_count=hop_count,
        )
        print("[{}] RREP -> origin={} hops={}".format(self.node_id, origin_id, hop_count))
        self.outstanding[(self.node_id, seq)] = time.time()
        self.send_packet(pkt)

    def _handle_routing_data(self, subtype, body, from_id):
        if subtype == constants.ROUTING_RREP:
            try:
                target_id, origin_seq, hop_count = packets.parse_rrep(body)
            except Exception:
                return

            # Cache the route to the target
            self.routes.set_route(target_id, from_id, hop_count)
            print("[{}] RREP: route({}) via {} hops={}".format(
                self.node_id, target_id, from_id, hop_count))

            # Clear the in-flight RREQ marker
            self._rreq_pending.pop(target_id, None)

            # Send any DATA packets that were buffered waiting for this route
            pending = self._pending_data.pop(target_id, [])
            for p in pending:
                print("[{}] flush pending DATA dst={} seq={}".format(
                    self.node_id, target_id, p.seq))
                self.send_packet(p)

    # ── ACK ───────────────────────────────────────────────────────────────────

    def _handle_ack(self, pkt, from_id):
        if len(pkt.payload) < 3:
            return
        orig_src = pkt.payload[0]
        orig_seq = int.from_bytes(pkt.payload[1:3], "big")
        key      = (orig_src, orig_seq)

        if orig_src == self.node_id and key in self.outstanding:
            # This ACK confirms a packet we originated
            print("[{}] ACK confirmed seq={}".format(self.node_id, orig_seq))
            del self.outstanding[key]
            disp = getattr(self, "display", None)
            if disp:
                try:
                    disp.update_on_ack(orig_seq)
                except Exception:
                    pass
            return

        prev = self.pending_forwards.pop(key, None)
        if prev is None:
            return
        # Relay ACK one hop further back toward the originator
        print("[{}] relay ACK orig_src={} seq={} -> prev={}".format(
            self.node_id, orig_src, orig_seq, prev))
        ack = packets.make_ack(
            src=self.node_id, dst=prev,
            orig_src=orig_src, orig_seq=orig_seq
        )
        self.send_packet(ack)

    # ── Hardware attach ───────────────────────────────────────────────────────

    def attach_hardware_from_config(self, config_path="config.json"):
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
        except Exception as e:
            print("[{}] cannot read config: {}".format(self.node_id, e))
            return False
        if not cfg.get("use_hardware"):
            return False
        try:
            from hw_adapter import HardwareRadio
        except Exception as e:
            print("[{}] hw_adapter import failed: {}".format(self.node_id, e))
            return False
        try:
            self.radio = HardwareRadio(self, cfg)
        except Exception as e:
            print("[{}] HardwareRadio init failed: {}".format(self.node_id, e))
            return False
        return True

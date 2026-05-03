"""Mesh node core: minimal state machine and forwarding logic used by simulator and later by hardware glue.

This module keeps the core logic intentionally small so it can be tested in-situ with `sim_harness.py`.
"""

import time
import json
from collections import deque
# avoid typing imports for MicroPython compatibility

import constants
import packets
from neighbour_table import NeighbourTable
from route_table import RouteTable


class Node:
    def __init__(self, node_id: int, allowlist: Optional[set] = None):
        self.node_id = node_id
        self.neighbours = NeighbourTable(allowlist=allowlist)
        self.routes = RouteTable()
        self.network = None  # set by SimNetwork.register_node
        self._seq = 1
        self.radio = None
        self.start_time = time.time()
        # duplicate suppression: (src, seq) -> timestamp
        self._seen = {}
        # pending forwards: (orig_src, orig_seq) -> prev_hop
        self.pending_forwards = {}
        # outstanding sends (originated by this node)
        self.outstanding = {}

    def next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        if self._seq == 0:
            self._seq = 1
        return s

    # --- network glue ---
    def send_packet(self, pkt: packets.Packet, to_next_hop: Optional[int] = None):
        # Log higher-level send intent
        try:
            print(f"[node {self.node_id}] send_packet src={pkt.src} dst={pkt.dst} seq={pkt.seq} kind={getattr(pkt, 'kind', '?')} to_next_hop={to_next_hop}")
        except Exception:
            pass
        b = pkt.to_bytes()
        if self.network is not None:
            if to_next_hop is None:
                # flood to all neighbours
                print(f"[node {self.node_id}] NET-FLOOD pkt src={pkt.src} dst={pkt.dst} seq={pkt.seq}")
                self.network.deliver(b, from_id=self.node_id)
            else:
                print(f"[node {self.node_id}] NET-SEND_DIRECT to {to_next_hop} pkt src={pkt.src} dst={pkt.dst} seq={pkt.seq}")
                self.network.send_direct(b, from_id=self.node_id, to_id=to_next_hop)
            return
        # hardware mode: send via radio if available (physical TX is always broadcast)
        if getattr(self, "radio", None) is not None:
            try:
                print(f"[node {self.node_id}] RADIO TX pkt src={pkt.src} dst={pkt.dst} seq={pkt.seq} len={len(b)}")
            except Exception:
                pass
            self.radio.send(b)
            return
        # fallback (debug)
        print(f"[node {self.node_id}] would send on radio: {pkt}")

    def send_beacon(self):
        seq = self.next_seq()
        # hops_to_ground unknown (255)
        pkt = packets.make_beacon(src=self.node_id, seq=seq, hops_to_ground=None)
        print(f"[node {self.node_id}] send_beacon seq={seq}")
        bt = getattr(self, "bt_logger", None)
        if bt:
            try:
                nei = len(self.neighbours.get_alive())
                bt.log("BCN seq={} nei={}".format(seq, nei))
            except Exception:
                pass
        self.send_packet(pkt)

    def send_data(self, dst, app_id, subtype, data=b"", ttl=constants.MAX_TTL):
        seq = self.next_seq()
        pkt = packets.make_data(src=self.node_id, dst=dst, seq=seq, ttl=ttl, app_id=app_id, subtype=subtype, data=data)
        # mark as outstanding so origin can detect final ACK
        self.outstanding[(self.node_id, seq)] = time.time()
        bt = getattr(self, "bt_logger", None)
        if bt:
            try:
                bt.log("TX dst={} app={} sub={} seq={}".format(dst, app_id, subtype, seq))
            except Exception:
                pass
        # try route first
        next_hop = self.routes.get_next_hop(dst)
        try:
            print(f"[node {self.node_id}] orig SEND DATA dst={dst} seq={seq} ttl={ttl} next_hop={next_hop}")
        except Exception:
            pass
        if next_hop:
            self.send_packet(pkt, to_next_hop=next_hop)
            # record that we forwarded this packet (prev_hop is None because we originated)
            self.pending_forwards[(self.node_id, seq)] = None
        else:
            # flood
            self.send_packet(pkt)

    # --- receiving / handling ---
    def receive_raw(self, data: bytes, from_id: Optional[int], rssi: Optional[int] = None, snr: Optional[int] = None):
        # physical RX diagnostics
        try:
            print(f"[node {self.node_id}] RX physical from={from_id} len={len(data)} rssi={rssi} snr={snr}")
        except Exception:
            pass
        try:
            pkt = packets.Packet.from_bytes(data)
        except Exception as e:
            print(f"[node {self.node_id}] bad packet from {from_id}: {e}")
            # debug logging removed from production; bad RX reported via prints
            return
        # update neighbour table using logical packet source when possible
        try:
            self.neighbours.update(pkt.src, rssi=rssi, snr=snr)
        except Exception:
            # fall back to physical identifier
            if from_id is not None:
                self.neighbours.update(from_id, rssi=rssi, snr=snr)
        # update display with last rx vitals if attached
        try:
            if getattr(self, "display", None):
                try:
                    self.display.update_on_rx(rssi=rssi, snr=snr, from_id=from_id, src=pkt.src)
                except Exception:
                    pass
        except Exception:
            pass
        # log the received packet at radio level
        # debug logging removed from production; RX vitals are handled by display
        self.handle_packet(pkt, from_id)

    def _seen_check(self, pkt: packets.Packet) -> bool:
        key = (pkt.src, pkt.seq)
        if key in self._seen:
            return True
        self._seen[key] = time.time()
        # prune occasionally
        if len(self._seen) > 1024:
            # remove oldest entries
            items = sorted(self._seen.items(), key=lambda x: x[1])
            for k, _ in items[: len(items) // 2]:
                del self._seen[k]
        return False

    def handle_packet(self, pkt: packets.Packet, from_id: int):
        # duplicate suppression for RREQ/DATA/BCAST
        if pkt.kind in (constants.KIND_DATA, constants.KIND_BCAST, constants.KIND_RREQ):
            if self._seen_check(pkt):
                return

        if pkt.kind == constants.KIND_BEACON:
            # payload may contain hops_to_ground
            if pkt.payload and pkt.payload[0] != 255:
                self.neighbours.update(pkt.src, rssi=None, snr=None, hops_to_ground=int(pkt.payload[0]))
            bt = getattr(self, "bt_logger", None)
            if bt:
                try:
                    bt.log("HEAR src={} seq={}".format(pkt.src, pkt.seq))
                except Exception:
                    pass
            return

        if pkt.kind == constants.KIND_ACK:
            # payload: orig_src, orig_seq
            if len(pkt.payload) >= 3:
                orig_src = pkt.payload[0]
                orig_seq = int.from_bytes(pkt.payload[1:3], "big")
                self._handle_ack(orig_src, orig_seq, from_id)
            return

        if pkt.kind == constants.KIND_DATA:
            # check if this node is the destination
            if pkt.dst == self.node_id:
                # deliver to app
                try:
                    app_id, subtype, body = packets.parse_app_payload(pkt.payload)
                except Exception:
                    app_id, subtype, body = (None, None, pkt.payload)
                print(f"[node {self.node_id}] DATA from {pkt.src} seq={pkt.seq} app={app_id} subtype={subtype} body={body}")
                bt = getattr(self, "bt_logger", None)
                if bt:
                    try:
                        bt.log("RX src={} app={} sub={} seq={}".format(pkt.src, app_id, subtype, pkt.seq))
                    except Exception:
                        pass
                # send ACK back to the neighbour we received from (prefer physical from_id if present)
                ack_dst = from_id if from_id is not None else pkt.src
                try:
                    print(f"[node {self.node_id}] sending ACK for orig_src={pkt.src} orig_seq={pkt.seq} dst={ack_dst} via_from_id={from_id}")
                except Exception:
                    pass
                ack = packets.make_ack(src=self.node_id, dst=ack_dst, orig_src=pkt.src, orig_seq=pkt.seq)
                # only request direct send in sim when from_id available
                self.send_packet(ack, to_next_hop=(from_id if self.network and from_id is not None else None))
                return

            # not for me: forward
            # record that when ACK returns we should send it back to the previous hop (physical if known)
            prev_hop = from_id if from_id is not None else pkt.src
            self.pending_forwards[(pkt.src, pkt.seq)] = prev_hop
            # decrement TTL and forward if possible
            if pkt.ttl <= 1:
                return
            pkt.ttl -= 1
            next_hop = self.routes.get_next_hop(pkt.dst)
            try:
                print(f"[node {self.node_id}] forward pkt src={pkt.src} dst={pkt.dst} seq={pkt.seq} ttl={pkt.ttl} prev_hop={prev_hop} next_hop={next_hop}")
            except Exception:
                pass
            if next_hop and self.network:
                self.send_packet(pkt, to_next_hop=next_hop)
            else:
                # networked sim: deliver manually to each neighbour except prev_hop
                if self.network:
                    b = pkt.to_bytes()
                    for n in self.network.topology.get(self.node_id, []):
                        if n == prev_hop:
                            continue
                        self.network.send_direct(b, from_id=self.node_id, to_id=n)
                else:
                    # hardware mode: just broadcast
                    self.send_packet(pkt)
            return

        # other kinds: not implemented yet

    def _handle_ack(self, orig_src: int, orig_seq: int, from_id: int):
        key = (orig_src, orig_seq)
        # if we originated the packet
        if orig_src == self.node_id and key in self.outstanding:
            print(f"[node {self.node_id}] delivery ACK received for seq={orig_seq}")
            # update display if attached
            try:
                if getattr(self, "display", None):
                    self.display.update_on_ack(orig_seq)
            except Exception:
                pass
            # production: ACK handling (no CSV logging)
            del self.outstanding[key]
            return
        prev = self.pending_forwards.pop(key, None)
        if prev is None:
            # either we didn't have a mapping, or this is the origin
            # if this node originated the payload it was handled above
            return
        # forward ACK to prev hop
        ack_dst = prev
        try:
            print(f"[node {self.node_id}] forwarding ACK orig_src={orig_src} orig_seq={orig_seq} to prev={prev}")
        except Exception:
            pass
        # production: ACK forwarded (no CSV logging)
        ack = packets.make_ack(src=self.node_id, dst=ack_dst, orig_src=orig_src, orig_seq=orig_seq)
        self.send_packet(ack, to_next_hop=(prev if self.network is not None else None))

    def attach_hardware_from_config(self, config_path: str = "config.json") -> bool:
        """Attach a HardwareRadio using the provided JSON config file.

        Returns True if the hardware adapter was attached, False otherwise.
        """
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[node {self.node_id}] cannot read config: {e}")
            return False
        if not cfg.get("use_hardware"):
            return False
        try:
            from hw_adapter import HardwareRadio
        except Exception as e:
            print(f"[node {self.node_id}] hw adapter import failed: {e}")
            return False
        try:
            self.radio = HardwareRadio(self, cfg)
        except Exception as e:
            print(f"[node {self.node_id}] hw adapter init failed: {e}")
            return False
        return True

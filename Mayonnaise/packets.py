"""Packet encoding/decoding for the mesh network.

Packet header (11 bytes):
  0        version     always 0x02
  1        kind        BEACON=1  DATA=2  BCAST=3  ACK=4
  2        src_id      original source (never changes during forwarding)
  3        dst_id      final destination (0xFF = broadcast)
  4        sender_id   node that physically transmitted this packet;
                       each forwarder overwrites this with its own ID so the
                       next hop knows who to ACK back to
  5        relay_id    designated next-hop forwarder; each forwarder looks up
                       its route and overwrites this before re-transmitting.
                       0xFF = any node may forward (BCAST / legacy)
  6        boot_id     originating node's boot ID (1-254); set once at origin,
                       never overwritten by forwarders — used for deduplication
  7-8      seq         16-bit per-source sequence counter (big-endian)
  9        ttl         decremented at each hop; drop at 0
  10       payload_len
  11+      payload

Application payload structure (DATA and BCAST):
  byte 0   app_id    APP_ROUTING=0  APP_LOCALISE=1  APP_CTRL=2  APP_THERM=3
  byte 1   subtype
  byte 2+  body

Routing payloads (app_id = APP_ROUTING):

  ROUTING_RREQ (BCAST):
    body[0]   target_id   node we are trying to reach
    body[1]   hop_count   incremented at each rebroadcast
    origin = pkt.src, origin_seq = pkt.seq  (read from header — no duplication)

  ROUTING_RREP (DATA, dst = origin_id):
    body[0]     target_id    the node that was found
    body[1-2]   origin_seq   matches the RREQ that triggered this (big-endian)
    body[3]     hop_count    total hops from target back to origin

  ROUTING_RECOVERY (BCAST):
    body[0]   lost_node_id   node that is no longer reachable
"""

import constants

MAX_PAYLOAD = 245   # 255 - 10 byte header; leaves headroom for LoRa frame overhead


class Packet:
    __slots__ = ("version", "kind", "src", "dst", "sender_id", "relay_id", "boot_id", "seq", "ttl", "payload")

    def __init__(self, kind, src, dst, seq, ttl, payload=b"", sender_id=0, relay_id=0xFF, boot_id=0, version=2):
        self.version   = version
        self.kind      = kind
        self.src       = src
        self.dst       = dst
        self.sender_id = sender_id
        self.relay_id  = relay_id
        self.boot_id   = boot_id
        self.seq       = seq
        self.ttl       = ttl
        self.payload   = payload

    def to_bytes(self):
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError("payload too large: {} > {}".format(len(self.payload), MAX_PAYLOAD))
        hdr = bytes([
            self.version   & 0xFF,
            self.kind      & 0xFF,
            self.src       & 0xFF,
            self.dst       & 0xFF,
            self.sender_id & 0xFF,
            self.relay_id  & 0xFF,
            self.boot_id   & 0xFF,
        ])
        hdr += int(self.seq).to_bytes(2, "big")
        hdr += bytes([self.ttl & 0xFF, len(self.payload)])
        return hdr + bytes(self.payload)

    @classmethod
    def from_bytes(cls, data):
        if len(data) < 11:
            raise ValueError("packet too short ({} bytes)".format(len(data)))
        version     = data[0]
        kind        = data[1]
        src         = data[2]
        dst         = data[3]
        sender_id   = data[4]
        relay_id    = data[5]
        boot_id     = data[6]
        seq         = int.from_bytes(data[7:9], "big")
        ttl         = data[9]
        payload_len = data[10]
        if len(data) < 11 + payload_len:
            raise ValueError("truncated payload")
        payload = data[11: 11 + payload_len]
        return cls(kind, src, dst, seq, ttl, payload, sender_id, relay_id, boot_id, version)

    def __repr__(self):
        return "Packet(kind={} src={} dst={} sender={} relay={} boot_id={} seq={} ttl={} payload_len={})".format(
            self.kind, self.src, self.dst, self.sender_id,
            self.relay_id, self.boot_id, self.seq, self.ttl, len(self.payload)
        )


# ── Factory helpers ───────────────────────────────────────────────────────────
# sender_id is left as 0 here; Node.send_packet() stamps it with self.node_id
# before transmission.

def make_beacon(src, seq, hops_to_ground=None):
    """1-hop broadcast. Payload: hops_to_ground (1 byte; 255 = unknown)."""
    h = 255 if (hops_to_ground is None) else (hops_to_ground & 0xFF)
    return Packet(constants.KIND_BEACON, src, constants.BROADCAST_ID, seq, 1, bytes([h]))


def make_data(src, dst, seq, ttl, app_id, subtype, data=b""):
    """Unicast DATA packet. Requires hop-by-hop ACK."""
    payload = bytes([app_id & 0xFF, subtype & 0xFF]) + bytes(data)
    return Packet(constants.KIND_DATA, src, dst, seq, ttl, payload)


def make_bcast(src, seq, ttl, app_id, subtype, data=b""):
    """Flood BCAST packet. No ACK. All nodes rebroadcast until TTL=0."""
    payload = bytes([app_id & 0xFF, subtype & 0xFF]) + bytes(data)
    return Packet(constants.KIND_BCAST, src, constants.BROADCAST_ID, seq, ttl, payload)


def make_ack(src, dst, orig_src, orig_seq):
    """Hop-by-hop ACK. dst = sender_id of the packet being acknowledged."""
    payload = bytes([orig_src & 0xFF]) + int(orig_seq).to_bytes(2, "big")
    return Packet(constants.KIND_ACK, src, dst, 0, 1, payload)


def make_rreq(src, seq, target_id, hop_count=0):
    """
    Route request flood. src/seq identify the origin and serve as the
    deduplication key — both are preserved unchanged by forwarders.
    hop_count is incremented in the payload at each hop.
    """
    body = bytes([target_id & 0xFF, hop_count & 0xFF])
    return make_bcast(src, seq, constants.MAX_TTL, constants.APP_ROUTING,
                      constants.ROUTING_RREQ, body)


def make_rrep(src, dst, seq, target_id, origin_seq, hop_count):
    """
    Route reply unicast. dst = origin_id (node that sent the RREQ).
    src = target_id (the node that was found).
    """
    body = bytes([target_id & 0xFF]) + int(origin_seq).to_bytes(2, "big") + bytes([hop_count & 0xFF])
    return make_data(src, dst, seq, constants.MAX_TTL,
                     constants.APP_ROUTING, constants.ROUTING_RREP, body)


def make_recovery(src, seq, lost_node_id):
    """Flood to inform local area that lost_node_id is no longer reachable."""
    body = bytes([lost_node_id & 0xFF])
    return make_bcast(src, seq, constants.MAX_TTL,
                      constants.APP_ROUTING, constants.ROUTING_RECOVERY, body)


# ── Payload parsers ───────────────────────────────────────────────────────────

def parse_app_payload(payload):
    """Return (app_id, subtype, body_bytes). Raises ValueError if too short."""
    if len(payload) < 2:
        raise ValueError("app payload too short")
    return payload[0], payload[1], payload[2:]


def parse_rreq(body):
    """Parse RREQ body (after app_id + subtype). Return (target_id, hop_count)."""
    if len(body) < 2:
        raise ValueError("rreq body too short")
    return body[0], body[1]


def parse_rrep(body):
    """Parse RREP body. Return (target_id, origin_seq, hop_count)."""
    if len(body) < 4:
        raise ValueError("rrep body too short")
    return body[0], int.from_bytes(body[1:3], "big"), body[3]


if __name__ == "__main__":
    # Self-test — run with: python packets.py
    import constants as c

    # Round-trip DATA
    p = make_data(1, 5, 123, 6, c.APP_LOCALISE, 1, b"hi")
    b = p.to_bytes()
    assert len(b) == 11 + 4, "wrong length"
    p2 = Packet.from_bytes(b)
    assert p2.src == 1 and p2.dst == 5 and p2.seq == 123
    assert p2.kind == c.KIND_DATA
    assert p2.relay_id == c.BROADCAST_ID

    # RREQ round-trip
    r = make_rreq(src=1, seq=10, target_id=7, hop_count=0)
    rb = r.to_bytes()
    r2 = Packet.from_bytes(rb)
    assert r2.kind == c.KIND_BCAST and r2.src == 1 and r2.seq == 10
    app_id, subtype, body = parse_app_payload(r2.payload)
    assert app_id == c.APP_ROUTING and subtype == c.ROUTING_RREQ
    target, hops = parse_rreq(body)
    assert target == 7 and hops == 0

    # RREP round-trip
    rp = make_rrep(src=7, dst=1, seq=20, target_id=7, origin_seq=10, hop_count=3)
    rpb = rp.to_bytes()
    rp2 = Packet.from_bytes(rpb)
    assert rp2.kind == c.KIND_DATA and rp2.src == 7 and rp2.dst == 1
    app_id, subtype, body = parse_app_payload(rp2.payload)
    target, origin_seq, hops = parse_rrep(body)
    assert target == 7 and origin_seq == 10 and hops == 3

    # sender_id / relay_id stamping
    p3 = make_data(1, 5, 1, 6, c.APP_CTRL, 1)
    p3.sender_id = 3
    p3.relay_id  = 5
    b3 = p3.to_bytes()
    p4 = Packet.from_bytes(b3)
    assert p4.sender_id == 3
    assert p4.relay_id  == 5

    print("packets: all self-tests passed")

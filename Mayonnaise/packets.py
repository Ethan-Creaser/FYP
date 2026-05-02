"""Packet encoding/decoding for the mesh core.

Envelope (bytes):
- version (1)
- kind (1)
- src_id (1)
- dst_id (1) ; 0xFF = broadcast
- seq (2)
- ttl (1)
- payload_len (1)
- payload (payload_len)

Application payload (first bytes): app_id (1), subtype (1), data...
"""

import constants


MAX_PAYLOAD = 255


class Packet:
    def __init__(self, kind, src, dst, seq, ttl, payload=b"", version=1):
        self.kind = kind
        self.src = src
        self.dst = dst
        self.seq = seq
        self.ttl = ttl
        self.payload = payload
        self.version = version

    def to_bytes(self):
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError("payload too large")
        header = bytes([
            self.version,
            self.kind,
            self.src,
            self.dst,
        ])
        header += int(self.seq).to_bytes(2, "big")
        header += bytes([self.ttl, len(self.payload)])
        return header + self.payload

    @classmethod
    def from_bytes(cls, data):
        if len(data) < 8:
            raise ValueError("packet too short")
        version = data[0]
        kind = data[1]
        src = data[2]
        dst = data[3]
        seq = int.from_bytes(data[4:6], "big")
        ttl = data[6]
        payload_len = data[7]
        if len(data) < 8 + payload_len:
            raise ValueError("invalid payload length")
        payload = data[8: 8 + payload_len]
        return cls(kind, src, dst, seq, ttl, payload, version)


def make_beacon(src, seq, hops_to_ground=None):
    # payload: optional single byte hops_to_ground (255 = unknown)
    if hops_to_ground is None:
        payload = bytes([255])
    else:
        payload = bytes([hops_to_ground & 0xFF])
    return Packet(constants.KIND_BEACON, src, constants.BROADCAST_ID, seq, 1, payload)


def make_data(src, dst, seq, ttl, app_id, subtype, data=b""):
    payload = bytes([app_id & 0xFF, subtype & 0xFF]) + data
    return Packet(constants.KIND_DATA, src, dst, seq, ttl, payload)


def make_ack(src, dst, orig_src, orig_seq):
    # ACK payload: orig_src (1), orig_seq (2)
    payload = bytes([orig_src & 0xFF]) + int(orig_seq).to_bytes(2, "big")
    return Packet(constants.KIND_ACK, src, dst, 0, 1, payload)


def parse_app_payload(payload):
    """Return (app_id, subtype, data_bytes)."""
    if len(payload) < 2:
        raise ValueError("app payload too short")
    return payload[0], payload[1], payload[2:]


if __name__ == "__main__":
    # quick self-test
    p = make_data(1, 5, 123, 6, constants.APP_LOCALISE, 1, b"hi")
    b = p.to_bytes()
    p2 = Packet.from_bytes(b)
    assert p2.src == 1 and p2.dst == 5 and p2.seq == 123
    print("packets: self-test ok")

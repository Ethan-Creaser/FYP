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

from __future__ import annotations
import typing
from dataclasses import dataclass
from typing import Optional
import constants


MAX_PAYLOAD = 255


@dataclass
class Packet:
    kind: int
    src: int
    dst: int
    seq: int
    ttl: int
    payload: bytes = b""
    version: int = 1

    def to_bytes(self) -> bytes:
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
    def from_bytes(cls, data: bytes) -> "Packet":
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
        payload = data[8 : 8 + payload_len]
        return cls(kind=kind, src=src, dst=dst, seq=seq, ttl=ttl, payload=payload, version=version)


def make_beacon(src: int, seq: int, hops_to_ground: Optional[int] = None) -> Packet:
    # payload: optional single byte hops_to_ground (255 = unknown)
    if hops_to_ground is None:
        payload = bytes([255])
    else:
        payload = bytes([hops_to_ground & 0xFF])
    return Packet(kind=constants.KIND_BEACON, src=src, dst=constants.BROADCAST_ID, seq=seq, ttl=1, payload=payload)


def make_data(src: int, dst: int, seq: int, ttl: int, app_id: int, subtype: int, data: bytes = b"") -> Packet:
    payload = bytes([app_id & 0xFF, subtype & 0xFF]) + data
    return Packet(kind=constants.KIND_DATA, src=src, dst=dst, seq=seq, ttl=ttl, payload=payload)


def make_ack(src: int, dst: int, orig_src: int, orig_seq: int) -> Packet:
    # ACK payload: orig_src (1), orig_seq (2)
    payload = bytes([orig_src & 0xFF]) + int(orig_seq).to_bytes(2, "big")
    return Packet(kind=constants.KIND_ACK, src=src, dst=dst, seq=0, ttl=1, payload=payload)


def parse_app_payload(payload: bytes) -> typing.Tuple[int, int, bytes]:
    """Return (app_id, subtype, data_bytes)."""
    if len(payload) < 2:
        raise ValueError("app payload too short")
    return payload[0], payload[1], payload[2:]


if __name__ == "__main__":
    # quick self-test
    p = make_data(src=1, dst=5, seq=123, ttl=6, app_id=constants.APP_LOCALISE, subtype=1, data=b"hi")
    b = p.to_bytes()
    p2 = Packet.from_bytes(b)
    assert p2.src == 1 and p2.dst == 5 and p2.seq == 123
    print("packets: self-test ok")

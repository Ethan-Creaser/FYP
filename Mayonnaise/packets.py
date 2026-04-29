try:
    import ujson as json
except ImportError:
    import json


VERSION = 1

HELLO = "HELLO"
HEARTBEAT = "HEARTBEAT"
SENSOR_REPORT = "SENSOR_REPORT"
RANGE_REPORT = "RANGE_REPORT"
ROVER_START = "ROVER_START"
ROVER_STOP = "ROVER_STOP"
LOCALISE_DISCOVERY = "LOCALISE_DISCOVERY"
LOCALISE_TURN = "LOCALISE_TURN"
LOCALISE_RESULT = "LOCALISE_RESULT"
LOCALISE_POSITION = "LOCALISE_POSITION"
IMAGE_OFFER = "IMAGE_OFFER"

BROADCAST = None


def make_packet(packet_type, src, dst, seq, ttl=5, payload=None):
    return {
        "v": VERSION,
        "t": packet_type,
        "src": src,
        "via": src,
        "dst": dst,
        "seq": seq,
        "ttl": ttl,
        "p": payload or {},
    }


def encode_packet(packet):
    return json.dumps(packet)


def decode_packet(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        packet = json.loads(raw)
    except Exception:
        return None

    required = ("v", "t", "src", "seq", "ttl")
    for key in required:
        if key not in packet:
            return None
    if packet.get("v") != VERSION:
        return None
    if "dst" not in packet:
        packet["dst"] = BROADCAST
    if "via" not in packet:
        packet["via"] = packet.get("src")
    if "p" not in packet:
        packet["p"] = {}
    return packet


def packet_uid(packet):
    return "{}:{}".format(packet.get("src"), packet.get("seq"))


def is_for_node(packet, node_id):
    dst = packet.get("dst")
    return dst is BROADCAST or dst == node_id


def relay_copy(packet, via_node_id):
    ttl = int(packet.get("ttl", 0))
    if ttl <= 0:
        return None

    relayed = packet.copy()
    relayed["via"] = via_node_id
    relayed["ttl"] = ttl - 1
    return relayed

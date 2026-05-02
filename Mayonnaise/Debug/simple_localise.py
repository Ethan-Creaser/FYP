"""
simple_localise.py

4-phase localisation — standalone, no EggNode framework needed.

Phase 1  DISCOVER  10 s  Blast beacons, record every egg heard
Phase 2  AGREE     10 s  Share local lists, build complete_network
Phase 3  ASSIGN          Sort by node_id, give UWB slot 0,1,2...
Phase 4  RANGE           Lowest egg = tag, rest = anchors, range + report

Run instead of main.py.  Needs identity.bin on the device.
Output lines read by PC_Scripts/pc_localisation.py:
    RANGE_DATA src=N  M:1.234  K:2.567
"""

import utime
import ujson
from machine import Pin
from neopixel import NeoPixel

from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.uwb.bu03 import BU03

# ------------------------------------------------------------------ #
# Config                                                               #
# ------------------------------------------------------------------ #

LORA_PARAMS = {
    "frequency":        433000000,
    "tx_power_level":   10,
    "signal_bandwidth": 125000,
    "spreading_factor": 9,
}

UWB_CHANNEL  = 1
UWB_RATE     = 1
UWB_FRAMES   = 15       # frames per scan
UWB_SETTLE   = 3000     # ms — anchors need time to detect new tag

DISCOVER_MS  = 10000    # Phase 1 duration
BEACON_MS    = 300      # how often to beacon during discovery
AGREE_MS     = 10000    # Phase 2 duration
SHARE_MS     = 1000     # how often to rebroadcast our list during agreement
RANGE_MS     = 15000    # delay between ranging rounds

LOCAL_NET_FILE    = "local_network.txt"
COMPLETE_NET_FILE = "complete_network.txt"

# ------------------------------------------------------------------ #
# Identity                                                             #
# ------------------------------------------------------------------ #

def load_identity():
    with open("identity.bin", "rb") as f:
        data = f.read(3)
    if len(data) != 3 or data[0] != 0xE9:
        raise RuntimeError("identity.bin missing or corrupt")
    return data[1], data[2]   # node_id, uwb_id (hardware slot, unused here)


# ------------------------------------------------------------------ #
# Radio helpers                                                        #
# ------------------------------------------------------------------ #

def tx(radio, msg_type, node_id, payload=None):
    pkt = {"t": msg_type, "src": node_id}
    if payload:
        pkt.update(payload)
    radio.send(ujson.dumps(pkt))


def rx(radio):
    raw = radio.poll_receive()
    if not raw:
        return None
    try:
        return ujson.loads(raw)
    except Exception:
        return None


# ------------------------------------------------------------------ #
# File helpers                                                         #
# ------------------------------------------------------------------ #

def save_list(path, items):
    with open(path, "w") as f:
        f.write(ujson.dumps(sorted(items)))


def load_list(path):
    try:
        with open(path, "r") as f:
            return ujson.loads(f.read())
    except Exception:
        return []


# ------------------------------------------------------------------ #
# Phase 1 — DISCOVER                                                   #
# ------------------------------------------------------------------ #

def phase_discover(radio, node_id):
    print("=== PHASE 1: DISCOVER ===")
    heard = set()
    deadline     = utime.ticks_add(utime.ticks_ms(), DISCOVER_MS)
    next_beacon  = utime.ticks_ms()

    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        now = utime.ticks_ms()

        # Send beacon
        if utime.ticks_diff(now, next_beacon) >= 0:
            tx(radio, "BEACON", node_id)
            next_beacon = utime.ticks_add(now, BEACON_MS)

        # Collect replies
        pkt = rx(radio)
        if pkt and pkt.get("t") == "BEACON":
            src = pkt.get("src")
            if src is not None and src != node_id and src not in heard:
                heard.add(src)
                print("  Heard egg_{}".format(src))

        utime.sleep_ms(10)

    peers = sorted(heard)
    save_list(LOCAL_NET_FILE, peers)
    print("  Local network: {}".format(peers))
    return peers


# ------------------------------------------------------------------ #
# Phase 2 — AGREE                                                      #
# ------------------------------------------------------------------ #

def phase_agree(radio, node_id, local_peers):
    print("=== PHASE 2: AGREE ===")
    complete = set(local_peers)
    complete.add(node_id)

    # Track which peers have shared their list with us
    confirmed = set()

    deadline    = utime.ticks_add(utime.ticks_ms(), AGREE_MS)
    next_share  = utime.ticks_ms()

    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        now = utime.ticks_ms()

        # Broadcast our current complete list
        if utime.ticks_diff(now, next_share) >= 0:
            tx(radio, "NET_SHARE", node_id, {"peers": sorted(complete)})
            next_share = utime.ticks_add(now, SHARE_MS)

        # Collect others' lists
        pkt = rx(radio)
        if pkt and pkt.get("t") == "NET_SHARE":
            src = pkt.get("src")
            peers = pkt.get("peers", [])
            if src is not None and src != node_id:
                confirmed.add(src)
                for p in peers:
                    if p not in complete:
                        complete.add(p)
                        print("  Added egg_{} (via egg_{})".format(p, src))

        # Done early if all known peers have confirmed
        if local_peers and confirmed.issuperset(set(local_peers)):
            print("  All peers confirmed")
            break

        utime.sleep_ms(10)

    result = sorted(complete)
    save_list(COMPLETE_NET_FILE, result)
    print("  Complete network: {}".format(result))
    return result


# ------------------------------------------------------------------ #
# Phase 3 — ASSIGN UWB SLOTS                                          #
# ------------------------------------------------------------------ #

def phase_assign(node_id, complete_network):
    print("=== PHASE 3: ASSIGN ===")
    # Sort ascending — lowest node_id gets slot 0
    ordered = sorted(complete_network)
    my_slot = ordered.index(node_id)

    # Build peer slot map: {node_id: uwb_slot}
    slots = {nid: i for i, nid in enumerate(ordered)}

    print("  Slot assignments: {}".format(slots))
    print("  My slot: {}  My role: {}".format(
        my_slot, "TAG (ranger)" if my_slot == 0 else "ANCHOR slot {}".format(my_slot)
    ))
    return my_slot, slots


# ------------------------------------------------------------------ #
# Phase 4 — RANGE                                                      #
# ------------------------------------------------------------------ #

def phase_range(uwb, radio, node_id, my_slot, slots):
    print("=== PHASE 4: RANGE ===")
    is_tag = (my_slot == 0)

    if is_tag:
        print("  Configuring as TAG (will range to all anchors)")
        uwb.configure(my_slot, role=0, channel=UWB_CHANNEL, rate=UWB_RATE)
    else:
        print("  Configuring as ANCHOR slot {}".format(my_slot))
        uwb.configure(my_slot, role=1, channel=UWB_CHANNEL, rate=UWB_RATE)

    if not is_tag:
        # Anchors just sit and wait — ranging handled by the tag
        print("  Anchor ready — waiting for tag to range us")
        return

    # Tag: range repeatedly
    while True:
        utime.sleep_ms(UWB_SETTLE)
        uwb.flush()
        raw = uwb.scan(frames=UWB_FRAMES)
        print("  UWB scan raw: {}".format(raw))

        # Map uwb_slot -> node_id and build distances dict
        slot_to_node = {v: k for k, v in slots.items()}
        distances = {}
        for slot, dist in raw.items():
            nid = slot_to_node.get(int(slot))
            if nid is not None and nid != node_id and dist > 0:
                distances[nid] = round(float(dist), 4)

        if distances:
            pairs = "  ".join("{}:{:.4f}".format(nid, d)
                              for nid, d in distances.items())
            # This line is parsed by PC_Scripts/pc_localisation.py
            print("RANGE_DATA src={}  {}".format(node_id, pairs))
        else:
            print("  No distances measured")

        utime.sleep_ms(RANGE_MS)


# ------------------------------------------------------------------ #
# LED helpers                                                          #
# ------------------------------------------------------------------ #

np = NeoPixel(Pin(38, Pin.OUT), 1)

def led(r, g, b):
    np[0] = (r, g, b)
    np.write()


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #

def main():
    led(255, 0, 0)   # red = booting

    node_id, _ = load_identity()
    print("simple_localise  node_id={}".format(node_id))

    radio = LoRaTransceiver(parameters=LORA_PARAMS)
    radio.start_receive()

    uwb = BU03(
        data_uart_id=1, data_tx=17, data_rx=18,
        config_uart_id=2, config_tx=2, config_rx=1,
        reset_pin=15,
    )

    led(255, 165, 0)   # orange = discovering

    local_peers    = phase_discover(radio, node_id)
    complete_net   = phase_agree(radio, node_id, local_peers)
    my_slot, slots = phase_assign(node_id, complete_net)

    led(0, 0, 255)   # blue = ranging

    phase_range(uwb, radio, node_id, my_slot, slots)


main()

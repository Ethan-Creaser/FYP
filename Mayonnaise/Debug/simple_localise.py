"""
simple_localise.py

TDMA-ordered localisation — minimal, two-egg friendly.

Phase 1  DISCOVER   Each node transmits exactly once in its time slot
                    (slot = node_id % 10 * SLOT_MS from radio-init).
                    No collisions. RSSI recorded for every heard beacon.

Phase 2  AGREE      Share local lists, merge complete network.

Phase 3  ASSIGN     Sort by node_id -> UWB slot 0, 1, 2 ...

Phase 4  RANGE      Slot-0 = tag, rest = anchors. Emit RANGE_DATA for PC.
                    If UWB fails, RSSI distances are already logged.

Output lines read by PC_Scripts/pc_localisation.py:
    RANGE_DATA src=N  M:1.234  K:2.567
    RSSI_DATA  src=N  M:-85:3.2  K:-72:1.8   (node_id:rssi_dBm:est_metres)
"""

import utime
import ujson
import math
from machine import Pin
from neopixel import NeoPixel

from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.uwb.bu03 import BU03
from Drivers.bt.bt_logger import BtLogger

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
UWB_FRAMES   = 15
UWB_SETTLE   = 3000     # ms — anchors need time to detect new tag

SLOT_MS      = 800      # time slot per node (must be > LoRa SF9 airtime ~200ms)
DISCOVER_MS  = 12000    # total discovery window
AGREE_MS     = 10000    # agreement window
SHARE_MS     = 1000     # re-broadcast list interval during agree
RANGE_MS     = 12000    # delay between ranging rounds

# RSSI path-loss model: d = 10 ^ ((RSSI_REF - rssi) / (10 * N))
# RSSI_REF: measured RSSI at 1 m with your antennas (tune this).
# PATH_N:   path-loss exponent (2.0 free space, 2.7 typical outdoor).
RSSI_REF     = -50
PATH_N       = 2.7

LOCAL_NET_FILE    = "local_network.txt"
COMPLETE_NET_FILE = "complete_network.txt"

# ------------------------------------------------------------------ #
# LED + logging                                                        #
# ------------------------------------------------------------------ #

np  = NeoPixel(Pin(38, Pin.OUT), 1)
_bt = None

def led(r, g, b):
    np[0] = (r, g, b)
    np.write()

def log(msg):
    print(msg)
    if _bt is not None:
        _bt.log(msg)

# ------------------------------------------------------------------ #
# Identity                                                             #
# ------------------------------------------------------------------ #

def load_identity():
    with open("identity.bin", "rb") as f:
        data = f.read(3)
    if len(data) != 3 or data[0] != 0xE9:
        raise RuntimeError("identity.bin missing or corrupt")
    return data[1], data[2]

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
        return None, None
    rssi = radio.last_rssi
    try:
        return ujson.loads(raw), rssi
    except Exception:
        return None, None

# ------------------------------------------------------------------ #
# RSSI -> rough distance                                               #
# ------------------------------------------------------------------ #

def rssi_to_m(rssi):
    try:
        return round(10 ** ((RSSI_REF - rssi) / (10.0 * PATH_N)), 2)
    except Exception:
        return 0.0

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
# Phase 1 — DISCOVER (TDMA slots)                                      #
# ------------------------------------------------------------------ #

def phase_discover(radio, node_id):
    """
    Each node transmits exactly once in its assigned slot.
    Slot offset = (node_id % 10) * SLOT_MS from now.
    All other time is spent listening.
    RSSI is recorded for every heard beacon.
    """
    log("=== PHASE 1: DISCOVER ===")

    heard   = {}   # node_id -> rssi
    t_start = utime.ticks_ms()

    my_slot_ms   = (node_id % 10) * SLOT_MS
    my_tx_at     = utime.ticks_add(t_start, my_slot_ms)
    my_tx_done   = False
    discover_end = utime.ticks_add(t_start, DISCOVER_MS)

    log("  My slot in {}ms".format(my_slot_ms))

    while utime.ticks_diff(discover_end, utime.ticks_ms()) > 0:
        now = utime.ticks_ms()

        # Transmit exactly once when our slot arrives
        if not my_tx_done and utime.ticks_diff(now, my_tx_at) >= 0:
            log("  Transmitting beacon")
            tx(radio, "BEACON", node_id)
            my_tx_done = True

        # Listen for other nodes' beacons
        pkt, rssi = rx(radio)
        if pkt and pkt.get("t") == "BEACON":
            src = pkt.get("src")
            if src is not None and src != node_id and src not in heard:
                dist = rssi_to_m(rssi)
                heard[src] = rssi
                log("  Heard egg_{}  RSSI={}dBm  ~{}m".format(src, rssi, dist))

        utime.sleep_ms(10)

    peers = sorted(heard.keys())
    save_list(LOCAL_NET_FILE, peers)
    log("  Local network: {}  rssi: {}".format(peers, heard))
    return peers, heard

# ------------------------------------------------------------------ #
# Phase 2 — AGREE                                                      #
# ------------------------------------------------------------------ #

def phase_agree(radio, node_id, local_peers):
    log("=== PHASE 2: AGREE ===")
    complete = set(local_peers)
    complete.add(node_id)
    confirmed = set()

    deadline   = utime.ticks_add(utime.ticks_ms(), AGREE_MS)
    next_share = utime.ticks_ms()

    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        now = utime.ticks_ms()

        if utime.ticks_diff(now, next_share) >= 0:
            tx(radio, "NET_SHARE", node_id, {"peers": sorted(complete)})
            next_share = utime.ticks_add(now, SHARE_MS)

        pkt, _ = rx(radio)
        if pkt and pkt.get("t") == "NET_SHARE":
            src   = pkt.get("src")
            peers = pkt.get("peers", [])
            if src is not None and src != node_id:
                confirmed.add(src)
                for p in peers:
                    if p not in complete:
                        complete.add(p)
                        log("  Added egg_{} via egg_{}".format(p, src))

        if local_peers and confirmed.issuperset(set(local_peers)):
            log("  All peers confirmed")
            break

        utime.sleep_ms(10)

    result = sorted(complete)
    save_list(COMPLETE_NET_FILE, result)
    log("  Complete network: {}".format(result))
    return result

# ------------------------------------------------------------------ #
# Phase 3 — ASSIGN                                                     #
# ------------------------------------------------------------------ #

def phase_assign(node_id, complete_network):
    log("=== PHASE 3: ASSIGN ===")
    ordered = sorted(complete_network)
    my_slot = ordered.index(node_id)
    slots   = {nid: i for i, nid in enumerate(ordered)}
    log("  Slots: {}".format(slots))
    log("  My slot: {}  Role: {}".format(
        my_slot,
        "TAG (ranger)" if my_slot == 0 else "ANCHOR slot {}".format(my_slot),
    ))
    return my_slot, slots

# ------------------------------------------------------------------ #
# Phase 4 — RANGE                                                      #
# ------------------------------------------------------------------ #

def phase_range(uwb, radio, node_id, my_slot, slots, rssi_map):
    log("=== PHASE 4: RANGE ===")

    # Emit RSSI-based rough map immediately — free, already have it
    if rssi_map:
        rssi_parts = "  ".join(
            "{}:{}:{:.2f}".format(nid, rssi, rssi_to_m(rssi))
            for nid, rssi in rssi_map.items()
        )
        log("RSSI_DATA src={}  {}".format(node_id, rssi_parts))

    is_tag = (my_slot == 0)

    if is_tag:
        log("  Configuring UWB as TAG")
        uwb.configure(my_slot, role=0, channel=UWB_CHANNEL, rate=UWB_RATE)
    else:
        log("  Configuring UWB as ANCHOR slot {}".format(my_slot))
        uwb.configure(my_slot, role=1, channel=UWB_CHANNEL, rate=UWB_RATE)

    if not is_tag:
        log("  Anchor holding — UWB listening for tag")
        while True:
            utime.sleep_ms(1000)

    # Tag: range loop
    slot_to_node = {v: k for k, v in slots.items()}

    while True:
        utime.sleep_ms(UWB_SETTLE)
        uwb.flush()
        raw = uwb.scan(frames=UWB_FRAMES)
        log("  UWB raw: {}".format(raw))

        distances = {}
        for slot, dist in raw.items():
            nid = slot_to_node.get(int(slot))
            if nid is not None and nid != node_id and dist > 0:
                distances[nid] = round(float(dist), 4)

        if distances:
            pairs = "  ".join("{}:{:.4f}".format(nid, d)
                              for nid, d in distances.items())
            log("RANGE_DATA src={}  {}".format(node_id, pairs))
        else:
            log("  No UWB distances (falling back to RSSI map)")
            if rssi_map:
                rssi_parts = "  ".join(
                    "{}:{}:{:.2f}".format(nid, rssi, rssi_to_m(rssi))
                    for nid, rssi in rssi_map.items()
                )
                log("RSSI_DATA src={}  {}".format(node_id, rssi_parts))

        utime.sleep_ms(RANGE_MS)

# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #

def main():
    global _bt
    led(255, 0, 0)

    node_id, _ = load_identity()
    node_name  = "egg_{}".format(node_id)

    try:
        _bt = BtLogger(name=node_name)
        print("BT: advertising as {}".format(node_name))
    except Exception as e:
        print("BT failed: {}".format(e))
        _bt = None

    log("simple_localise  node={}  slot_ms={}".format(node_id, SLOT_MS))

    radio = LoRaTransceiver(parameters=LORA_PARAMS)
    radio.start_receive()
    log("LoRa ready")

    uwb = BU03(
        data_uart_id=1, data_tx=17, data_rx=18,
        config_uart_id=2, config_tx=2, config_rx=1,
        reset_pin=15,
    )
    log("UWB ready")

    led(255, 165, 0)

    local_peers, rssi_map  = phase_discover(radio, node_id)
    complete_net           = phase_agree(radio, node_id, local_peers)
    my_slot, slots         = phase_assign(node_id, complete_net)

    led(0, 0, 255)

    phase_range(uwb, radio, node_id, my_slot, slots, rssi_map)


main()

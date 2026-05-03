"""
Minimal two-node localisation role-assignment test.

Flash the SAME script on both nodes. Change only NODE_ID and UWB_ID.

Flow:
  1. Both nodes cold-configure UWB as anchor and broadcast HELLO over LoRa.
  2. Each node waits to hear the other's HELLO (DISCOVERY_MS window).
  3. Lower node_id wins coordinator role.
  4. Coordinator → tag → scans UWB → anchor → sends TURN to follower.
  5. Follower receives TURN → tag → scans UWB → anchor → sends RESULT back.
  6. Both nodes print what they measured and stop.
"""

import utime
from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.uwb.bu03 import BU03

# ---- set these per-node ----
NODE_ID  = 0   # logical node id
UWB_ID   = 0   # UWB hardware id (0-7)
# ----------------------------

CHANNEL        = 1
RATE           = 1
DISCOVERY_MS   = 10000   # how long to broadcast HELLO and listen
HELLO_EVERY_MS = 1500
TURN_TIMEOUT_MS = 20000  # follower waits this long for a TURN
RESULT_TIMEOUT_MS = 20000

LORA_PARAMS = {
    "frequency":        433000000,
    "tx_power_level":   10,
    "signal_bandwidth": 125000,
    "spreading_factor": 9,
}


def log(msg):
    print("[{}] {}".format(NODE_ID, msg))


# ---------- init hardware ----------

log("Initialising LoRa...")
radio = LoRaTransceiver(parameters=LORA_PARAMS)
log("LoRa OK")

log("Cold-configuring UWB as anchor (id={})...".format(UWB_ID))
t0 = utime.ticks_ms()
uwb = BU03(
    data_uart_id=1, data_tx=17, data_rx=18,
    config_uart_id=2, config_tx=2, config_rx=1,
    reset_pin=15,
)
uwb.configure(UWB_ID, role=1, channel=CHANNEL, rate=RATE)
log("UWB anchor ready ({} ms)".format(utime.ticks_diff(utime.ticks_ms(), t0)))


# ---------- discovery ----------

def send_hello():
    radio.send("HELLO:{}:{}".format(NODE_ID, UWB_ID))

peers = {}   # node_id -> uwb_id

log("Discovery started ({} ms window)...".format(DISCOVERY_MS))
deadline = utime.ticks_add(utime.ticks_ms(), DISCOVERY_MS)
last_hello = None

while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
    now = utime.ticks_ms()
    if last_hello is None or utime.ticks_diff(now, last_hello) >= HELLO_EVERY_MS:
        send_hello()
        last_hello = now

    msg = radio.poll_receive()
    if msg and msg.startswith("HELLO:"):
        parts = msg.split(":")
        if len(parts) == 3:
            try:
                peer_node = int(parts[1])
                peer_uwb  = int(parts[2])
                if peer_node != NODE_ID and peer_node not in peers:
                    peers[peer_node] = peer_uwb
                    log("Discovered peer node={} uwb={}".format(peer_node, peer_uwb))
            except Exception:
                pass

    utime.sleep_ms(50)

if not peers:
    log("No peers found. Stopping.")
    raise SystemExit

all_ids = sorted(list(peers.keys()) + [NODE_ID])
coordinator = all_ids[0]
log("Coordinator elected: node {}  (I am {})".format(
    coordinator, "COORDINATOR" if coordinator == NODE_ID else "FOLLOWER"))


# ---------- helper: measure distances as tag ----------

def measure_as_tag(members):
    """Switch to tag, scan, return {node_id: distance_m}, restore anchor."""
    log("Switching to TAG for ranging...")
    t0 = utime.ticks_ms()
    uwb.configure_warm(UWB_ID, role=0, channel=CHANNEL, rate=RATE)
    log("  configure_warm(tag) took {} ms".format(utime.ticks_diff(utime.ticks_ms(), t0)))

    uwb.flush()
    raw = uwb.scan(frames=10)
    log("  Raw scan result: {}".format(raw))

    distances = {}
    for peer_node, peer_uwb in members.items():
        d = raw.get(peer_uwb)
        if d is not None and d > 0:
            distances[peer_node] = round(float(d), 4)

    log("Switching back to ANCHOR...")
    t0 = utime.ticks_ms()
    uwb.configure_warm(UWB_ID, role=1, channel=CHANNEL, rate=RATE)
    log("  configure_warm(anchor) took {} ms".format(utime.ticks_diff(utime.ticks_ms(), t0)))
    return distances


# ---------- coordinator path ----------

if coordinator == NODE_ID:
    log("--- COORDINATOR: measuring own distances ---")
    my_distances = measure_as_tag(peers)
    log("My distances: {}".format(my_distances))

    for peer_node in peers:
        log("Sending TURN to node {}...".format(peer_node))
        radio.send("TURN:{}:{}".format(NODE_ID, peer_node))

        log("Waiting for RESULT from node {}...".format(peer_node))
        deadline = utime.ticks_add(utime.ticks_ms(), RESULT_TIMEOUT_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            msg = radio.poll_receive()
            if msg and msg.startswith("RESULT:{}:".format(peer_node)):
                log("Got: {}".format(msg))
                break
            utime.sleep_ms(50)
        else:
            log("TIMEOUT waiting for RESULT from node {}".format(peer_node))

    log("=== COORDINATOR DONE ===")


# ---------- follower path ----------

else:
    log("--- FOLLOWER: waiting for TURN from coordinator {} ---".format(coordinator))
    deadline = utime.ticks_add(utime.ticks_ms(), TURN_TIMEOUT_MS)
    got_turn = False

    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        msg = radio.poll_receive()
        if msg and msg.startswith("TURN:{}:{}".format(coordinator, NODE_ID)):
            log("Received TURN signal")
            got_turn = True
            break
        utime.sleep_ms(50)

    if not got_turn:
        log("TIMEOUT waiting for TURN. Stopping.")
        raise SystemExit

    my_distances = measure_as_tag(peers)
    log("My distances: {}".format(my_distances))

    result_str = "RESULT:{}:{}".format(NODE_ID, my_distances)
    radio.send(result_str)
    log("Sent RESULT to coordinator")

    log("=== FOLLOWER DONE ===")

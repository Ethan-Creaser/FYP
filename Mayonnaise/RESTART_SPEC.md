Restart Spec — Mayonnaise Mesh v1
==================================

Summary
-------
Minimal, decoupled restart for the 14-node LoRa/UWB mesh. Goal: a stable,
testable mesh core that any two eggs can communicate through, with a clean
interface for the localisation module. Stability > speed in all tradeoffs.

What is out of scope for v1
---------------------------
- Deep sleep / low power
- Packet fragmentation
- Encryption / authentication
- Temperature telemetry (stretch goal, lowest priority)
- Full distance-vector routing


1. Node Identity
-----------------
Each egg has two IDs with different scopes:

  mesh_id (node_id)  : 1–14 for eggs, 99 for ground station
                       Used in all LoRa packet fields (src, dst, etc.)

  uwb_id             : 0–7 (hardware constraint: BU03 only supports 8 values)
                       Used only by the localisation layer for UWB ranging.
                       Only nodes with an assigned uwb_id participate in ranging.
                       Remaining nodes still mesh over LoRa normally.

Identity is stored in identity.bin on the device filesystem (3 bytes):
  [0xE9, node_id, uwb_id]

Written once per device using hardcode_egg_id.py. Survives firmware updates
as long as the filesystem is not erased. Falls back to config.json if missing.

config.json has node_id = null; the same file is flashed to every egg.

Config fields relevant to mesh:
  {
    "node_id": null,               // always null — identity.bin is the source
    "ground_station_id": 99,
    "use_hardware": true,
    "allowed_neighbors": null      // test-only; set per-node for indoor testing
  }

Assigning uwb_ids across 14 nodes:
- Assign uwb_id 0–7 to the 8 nodes most important for localisation geometry.
- Other 6 nodes have uwb_id matching their node_id (mesh relay only).
- The mesh core does not read uwb_id — it is a localisation-layer concern.


2. Packet Schema
-----------------
Binary envelope, 9-byte fixed header. All fields unsigned. Big-endian multi-byte.

  Byte 0   : version      always 0x01
  Byte 1   : kind         see table below
  Byte 2   : src_id       original source — never changes during forwarding
  Byte 3   : dst_id       final destination (0xFF = broadcast)
  Byte 4   : sender_id    node that physically transmitted this packet;
                          each forwarder overwrites this with its own ID so the
                          next hop knows who to ACK back to
  Byte 5-6 : seq          16-bit per-source sequence counter (big-endian)
  Byte 7   : ttl          decremented at each hop; drop at 0
  Byte 8   : payload_len
  Bytes 9+ : payload      (payload_len bytes)

4-type kind schema:
  BEACON = 0x01   1-hop liveness broadcast. TTL=1. No ACK.
  DATA   = 0x02   Unicast. Hop-by-hop ACK + bounded retry.
  BCAST  = 0x03   Limited flood. No ACK. Duplicate suppression + TTL.
  ACK    = 0x04   Hop-by-hop acknowledgement.

Routing control (RREQ, RREP, RECOVERY) is NOT a separate kind — it is
carried as subtypes inside BCAST and DATA using app_id = APP_ROUTING (0x00).
This means RREP inherits DATA's hop-by-hop ACK + retry for free.

Total header overhead: 9 bytes. Keep payloads under ~246 bytes.
No fragmentation. If a payload does not fit, it is an application design error.

Duplicate suppression: every node keeps a (src_id, seq) → timestamp cache.
Drop any packet whose (src_id, seq) has been seen recently. RREQ src/seq
never changes during flooding so deduplication works correctly.


3. Application Payload (inside DATA or BCAST)
----------------------------------------------
First two bytes of payload identify the application layer:

  Byte 0 : app_id
  Byte 1 : subtype
  Bytes 2+: body

app_id values:
  APP_ROUTING  = 0x00   Routing control — transparent to the application layer
  APP_LOCALISE = 0x01   UWB localisation payloads
  APP_CTRL     = 0x02   Commands and health checks
  APP_THERM    = 0x03   Temperature telemetry (stretch goal)

Routing subtypes (app_id = APP_ROUTING):
  ROUTING_RREQ     = 0x01   Route request — carried in BCAST
  ROUTING_RREP     = 0x02   Route reply   — carried in DATA (unicast)
  ROUTING_RECOVERY = 0x03   Topology change — carried in BCAST

RREQ body (after app_id + subtype bytes):
  Byte 0 : target_id    node we are trying to reach
  Byte 1 : hop_count    incremented at each rebroadcast
  origin and origin_seq are read from pkt.src and pkt.seq (no duplication)

RREP body (after app_id + subtype bytes):
  Byte 0   : target_id    the node that was found
  Byte 1-2 : origin_seq   matches the RREQ that triggered this (big-endian)
  Byte 3   : hop_count    total hops from target back to origin

RECOVERY body (after app_id + subtype bytes):
  Byte 0 : lost_node_id   node that is no longer reachable

ACK payload (kind = ACK):
  Byte 0   : orig_src   source of the packet being acknowledged
  Byte 1-2 : orig_seq   seq of the packet being acknowledged (big-endian)

BEACON payload:
  Byte 0 : hops_to_ground   0 if this node is the ground station; 255 if unknown

Localisation subtypes (app_id = APP_LOCALISE) — defined by teammate:
  LOC_RANGE_REQ  = 0x01   Request a UWB range measurement
  LOC_RANGE_RESP = 0x02   Reply carrying measured distance
  LOC_POSITION   = 0x03   Broadcast computed position estimate


4. Neighbour Table
-------------------
One entry per direct neighbour heard within LOST_TIMEOUT seconds.

  node_id           : mesh_id of the neighbour
  last_seen         : timestamp of last received packet (seconds)
  rssi              : last RSSI (dBm)
  snr               : last SNR (dB)
  hops_to_ground    : from neighbour's last BEACON; None if unknown
  is_alive          : False once LOST_TIMEOUT exceeded

Allowlist filtering: if allowed_neighbors is configured, packets from senders
not in the list are dropped immediately in receive_raw() — before any routing
or neighbour table update. This enforces artificial topology in the lab.

Timers:
  BEACON_INTERVAL : 30 s ± up to 5 s jitter (suppressed if any TX within interval)
  SUSPECT_TIMEOUT : 90 s
  LOST_TIMEOUT    : 150 s  → triggers RECOVERY flood and route invalidation

Beacon suppression: if this node transmitted anything within the last
BEACON_INTERVAL seconds, all nearby neighbours already refreshed their timers
from that broadcast. Skip the beacon to avoid redundant transmissions.


5. Route Table
---------------
Cached next-hop per destination.

  dest      : target mesh_id
  next_hop  : which direct neighbour to send to
  hops      : estimated total hops to destination
  last_used : timestamp
  failures  : consecutive delivery failures on this route

Policy:
  - Route cache TTL: 300 s. Refreshed on each successful use.
  - Invalidate when: failures >= 3, OR next_hop transitions to LOST.
  - DATA forwarding is TTL-based flooding (not route-table lookup) — only
    the originating node uses the route table to select the first hop.


6. Reliability (ACK and Retry)
--------------------------------
Hop-by-hop only. Each forwarding node ACKs its upstream sender; the
originator does not wait for an end-to-end ACK.

ACK uses sender_id (byte 4 of header) to identify the physical last-hop
transmitter. Each forwarder stamps its own node_id into sender_id before
transmitting, so the receiver always knows who to ACK back to regardless
of whether it is the original src.

Needs ACK + retry:  DATA (unicast), RREP
No ACK:             BEACON, RREQ (flooded), BCAST, RECOVERY (flooded)

Retry policy (managed by node.tick()):
  HOP_ACK_TIMEOUT  = 5 s    wait before first retry
  MAX_HOP_RETRIES  = 3      retries before giving up
  After max retries: routes.penalize(dst) — 3 failures deletes the route —
  then flood fresh RREQ.

Duplicate DATA re-ACK: if a DATA retry arrives and this node is the
destination, resend the ACK but do not re-deliver to the application. This
ensures the sender can clear its retry state even when the original ACK
was lost.


7. Periodic Maintenance (tick)
--------------------------------
node.tick() is called every 5 seconds by main.py. It does three things:

  1. Age neighbours
     Iterates neighbour table. Any entry with last_seen older than
     LOST_TIMEOUT that is still marked alive transitions to LOST.
     For each newly-lost neighbour: invalidate all routes through it,
     flood a RECOVERY BCAST.

  2. Retry outstanding packets
     Iterates packets waiting for hop-by-hop ACK. If HOP_ACK_TIMEOUT
     exceeded: resend (up to MAX_HOP_RETRIES). After max retries:
     penalise route, flood RREQ for the destination.

  3. Retry stale RREQs
     If a RREQ has been in-flight longer than RREQ_TIMEOUT with no RREP:
     re-flood (up to RREQ_MAX_ATTEMPTS). After max attempts: drop buffered
     DATA for that destination.


8. Route Discovery (RREQ / RREP)
----------------------------------
Triggered by send_data() when no cached route exists for the destination.

1. Node A wants to send DATA to node Z. No cached route.
2. DATA packet buffered (up to 3 per destination).
3. A floods RREQ BCAST (src=A, seq=N, target=Z, hop_count=0).
4. Each intermediate node hearing the RREQ for the first time:
   - Caches reverse route: route(A) → via from_id, hops+1
   - Increments hop_count in payload and rebroadcasts (TTL-based)
5. Node Z receives RREQ, sends RREP DATA unicast toward A.
6. Each node forwarding the RREP caches forward route: route(Z) → via from_id.
7. A receives RREP, caches route(Z), flushes buffered DATA.

RREQ_TIMEOUT = 3 s, RREQ_MAX_ATTEMPTS = 2. After failure: buffered DATA dropped.


9. Artificial Topology (Indoor Testing)
-----------------------------------------
Per-node config field: "allowed_neighbors": [id, id, ...]

Packets from senders not in the list are dropped in receive_raw() before any
processing. This enforces multi-hop paths in a lab where all nodes are
physically within LoRa range of each other.

Example 3-node chain (nodes 1–2–3, all physically in range):
  egg 1: "allowed_neighbors": [2]
  egg 2: "allowed_neighbors": [1, 3]
  egg 3: "allowed_neighbors": [2]

Set to null for outdoor / production deployment.


10. Localisation Interface
---------------------------
The mesh core is transport-only for localisation. It does not interpret
coordinates, distances, or ranging results.

Teammate subclasses LocaliseApp from app_localise.py:

  from app_localise import LocaliseApp, LOC_RANGE_REQ, LOC_RANGE_RESP

  class MyLoc(LocaliseApp):
      def on_rx(self, src_mesh_id, subtype, payload):
          # called automatically when an APP_LOCALISE packet arrives
          ...

  loc = MyLoc(node)   # registers itself; no other wiring needed

To send a ranging payload through the mesh:
  loc.send(dst_mesh_id, subtype, payload_bytes)

The mesh handles all routing, retries, and ACKs transparently.
main.py instantiates LocaliseApp (base class) at boot if localisation_enabled
is true in config; teammate replaces this with their subclass.


11. Ground Station
-------------------
node_id = 99. Behaves as a normal mesh node at the network layer.
Application layer extras:

  - Accepts CTRL packets (commands out, health responses in)
  - Collects DATA from all sources for report logging
  - Does not require UWB hardware (uwb_id can match node_id)
  - hops_to_ground = 0; advertised in every BEACON


12. File / Module Layout
--------------------------
  main.py               startup, config load, boot banner, node main loop
  node.py               mesh node: routing, forwarding, tick(), recovery
  packets.py            encode/decode all packet types (9-byte header)
  neighbour_table.py    neighbour table: update, age-out, allowlist
  route_table.py        route cache: lookup, insert, penalise, invalidate
  constants.py          all timing and protocol constants
  version.py            firmware version string — bump before each flash
  identity.py           read/write identity.bin (node_id, uwb_id)
  hardcode_egg_id.py    run once per device to write identity.bin
  app_localise.py       localisation bridge — teammate subclasses this
  topology.py           artificial topology loader (test-only)
  hw_adapter.py         SX1278 radio driver wrapper (MicroPython only)
  config.json           shared config — node_id = null (identity.bin is source)

  Debug/
    sim_harness.py      PC-side in-process mesh simulator
    hw_runner.py        on-device periodic send/ACK hardware test

  Drivers/
    lora/               SX1278 LoRa driver
    bt/                 BLE logger
    uwb/                BU03 UWB ranging driver (teammate's module)


13. MVP Success Criteria
--------------------------
v1 is complete when all of the following work on hardware:

  1. Neighbour discovery: eggs hear each other and build neighbour tables  ✓
  2. Forced multi-hop: indoor allowlist topology produces real 2-hop paths  ✓
  3. Route discovery: RREQ/RREP completes across a chain                   ✓
  4. Unicast delivery: DATA reaches destination with hop-by-hop ACK        ✓
  5. Local recovery: node removed → LOST detected → RECOVERY flooded       ✓
  6. Retry: unACKed packets retried; route penalised after max failures     ✓
  7. Localisation transport: app_localise.py bridges teammate's UWB code   (pending integration)


14. Constants Reference
------------------------
All timing values are in seconds.

  VERSION            = see version.py (currently "0.4.0")
  BROADCAST_ID       = 0xFF
  GROUND_STATION_ID  = 99

  MAX_TTL            = 6
  HOP_ACK_TIMEOUT    = 5      seconds before retrying an unACKed hop
  MAX_HOP_RETRIES    = 3
  RREQ_TIMEOUT       = 3      seconds to wait for RREP before retrying
  RREQ_MAX_ATTEMPTS  = 2

  BEACON_INTERVAL    = 30
  BEACON_JITTER      = 5
  SUSPECT_TIMEOUT    = 90
  LOST_TIMEOUT       = 150

  DEFAULT_ROUTE_TTL_MS = 300000   (5 minutes, kept in ms for comparison)
  MAX_ROUTE_FAILURES   = 3

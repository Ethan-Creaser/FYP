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

  mesh_id   : 1–14 for eggs, 99 for ground station
              Used in all LoRa packet fields (src, dst, next_hop, etc.)

  uwb_id    : 0–7 (hardware constraint: BU03 only supports 8 values)
              Used only by the localisation layer for UWB ranging.
              Only nodes with an assigned uwb_id participate in ranging.
              Remaining nodes still mesh over LoRa normally.

Config per node (e.g. node_config.json):

  {
    "mesh_id": 5,
    "uwb_id": 4,           // omit or null if this node has no UWB role
    "allowed_neighbors": [3, 7, 9]   // test-only; omit for outdoor/production
  }

Assigning uwb_ids across 14 nodes:
- Assign uwb_id 0–7 to the 8 nodes most important for localisation anchor geometry.
- The other 6 nodes have uwb_id = null and act as mesh relays only.
- Localisation coordinator tracks which uwb_ids are active.
- This is a localisation-layer concern; the mesh core does not read uwb_id.


2. Packet Schema
-----------------
Binary envelope. All fields are unsigned unless noted. Big-endian multi-byte.

  Byte 0   : version      (always 0x01 for v1)
  Byte 1   : kind         (see table below)
  Byte 2   : src_id
  Byte 3   : dst_id       (0xFF = broadcast)
  Byte 4-5 : seq          (16-bit, per-source counter)
  Byte 6   : ttl
  Byte 7   : payload_len
  Bytes 8+ : payload      (payload_len bytes)

  kind values:
    BEACON   = 0x01
    RREQ     = 0x02   route request
    RREP     = 0x03   route reply
    DATA     = 0x04   unicast application data
    BCAST    = 0x05   limited broadcast
    ACK      = 0x06
    RECOVERY = 0x07

Total overhead: 8 bytes. Keep payloads under ~200 bytes (LoRa SF-dependent).
No fragmentation. If a payload does not fit, it is an application design error.

Duplicate suppression: every node keeps a recent-seen cache of (src_id, seq)
pairs. Drop any packet whose (src_id, seq) has been seen in the last ~60 s.


3. Application Payload (inside DATA or BCAST)
----------------------------------------------
First two bytes identify the application layer:

  Byte 0 : app_id    LOCALISE=0x01, CTRL=0x02, THERM=0x03
  Byte 1 : subtype
  Bytes 2+: data

Localisation subtypes (app_id = 0x01):
  0x01 RANGE_CMD
  0x02 DIST_REPORT
  0x03 MAP_REQUEST
  0x04 MAP_UPDATE
  0x05 CROSS_RANGE
  0x06 CROSS_REPORT
  0x07 MERGE_REQ
  0x08 MERGE_POSITIONS

Control subtypes (app_id = 0x02):
  0x01 HEALTH_CHECK_REQ
  0x02 HEALTH_CHECK_RSP
  0x03 COMMAND

ACK payload (kind = ACK):
  Byte 0-1 : acked_seq   (seq being acknowledged)
  Byte 2   : acked_src
  Byte 3   : acked_dst

RREQ payload:
  Byte 0   : target_id   (node we are trying to reach)
  Byte 1   : origin_id   (node that started the discovery)
  Byte 2   : origin_seq  (discovery round ID)
  Byte 3   : hop_count   (incremented at each hop)

RREP payload:
  Byte 0   : target_id
  Byte 1   : origin_id
  Byte 2   : origin_seq
  Byte 3   : hop_count   (total hops in discovered path)


4. Neighbour Table
-------------------
One entry per direct neighbour that has been heard.

  node_id           : mesh_id of the neighbour
  last_seen_ms      : monotonic timestamp of last received packet
  rssi              : last RSSI (signed int8, dBm)
  snr               : last SNR (signed int8, dB)
  link_success_rate : rolling ACK success, 0–255 (255 = 100%)
  hops_to_ground    : from neighbour's last BEACON; 0 if neighbour is GS
  is_alive          : True if not yet LOST
  allowlisted       : derived from allowed_neighbors config (test-only)

Timers:
  BEACON_INTERVAL : 30 s ± up to 5 s random jitter
  SUSPECT_TIMEOUT : 90 s   (last_seen older than this → suspect)
  LOST_TIMEOUT    : 150 s  (last_seen older than this → lost, trigger recovery)

Only allowlisted neighbours (or all neighbours if no allowlist is configured)
are eligible as next-hops.


5. Route Table
---------------
Cached next-hop per destination.

  dest_id     : target mesh_id
  next_hop    : which direct neighbour to send to
  hop_count   : estimated total hops to destination
  last_used   : monotonic timestamp
  failures    : consecutive forward failures on this route

Policy:
  - Preferred next-hop: alive + allowlisted, lowest hop_count,
    tie-break by link_success_rate, then RSSI.
  - Route cache TTL: 300 s (5 min). Refresh on successful use.
  - Invalidate early if: failures >= 3, OR next_hop transitions to LOST.
  - Default TTL for forwarded DATA: 6 (decremented each hop, drop at 0).


6. Reliability (ACK and Retry)
--------------------------------
Hop-by-hop only. Each forwarding node ACKs its upstream; the originator
does not wait for an end-to-end ACK.

  Needs ACK + retry:
    DATA (unicast)
    RREP
    RECOVERY unicast
    localisation unicast (app_id=0x01 sent to a specific dst)

  No ACK:
    BEACON
    RREQ (flooded)
    BCAST (broadcast ACK causes storms — never ACK a broadcast)
    low-priority telemetry if it will be re-sent on the next cycle

Retry policy:
  MAX_HOP_RETRIES = 3
  Backoff: small fixed delay (e.g. 200 ms) between retries.
  After 3 failures: increment route.failures; try next-best neighbour if
  available; otherwise trigger RECOVERY.


7. Buffering and Queue Priority
---------------------------------
Small, bounded queues only. No unbounded buffering.

  Priority 1 (highest): ACK, RREP, RECOVERY
  Priority 2          : DATA (commands, localisation unicast)
  Priority 3          : BCAST (discovery, announcements)
  Priority 4 (lowest) : THERM telemetry

Telemetry policy: newest sample wins. Drop the oldest sample when full.
Commands/localisation: buffer for up to 30 s before dropping with a log.


8. Node State Machine
----------------------
Three top-level states:

  IDLE
    - Listening for incoming packets
    - Sending BEACON on interval
    - Processing received packets (deliver, forward, or drop)

  SENDING
    - Actively attempting to forward one packet with retries
    - Returns to IDLE on ACK received or retries exhausted

  RECOVERY
    - Triggered when a neighbour is LOST, a new neighbour appears,
      or a route fails 3 times
    - Sends a RREQ flood for affected destination(s)
    - Clears stale route table entries for affected next-hops
    - Returns to IDLE when RREP received or discovery timeout

Events and handlers:

  on_beacon_rx(pkt)
    → update neighbour table (last_seen, rssi, snr, hops_to_ground)
    → if new neighbour: trigger local recovery

  on_rreq_rx(pkt)
    → if already seen (src, origin_seq): drop (duplicate suppression)
    → if dst == me: send RREP back toward origin
    → else: decrement TTL, rebroadcast if TTL > 0

  on_rrep_rx(pkt)
    → if dst == me: cache route (target → next_hop = prev_hop, hops)
    → else: forward toward origin via route table

  on_data_rx(pkt)
    → if dst == me: deliver to application layer
    → else: look up route, forward or trigger RREQ if no route

  on_ack_rx(pkt)
    → mark hop success, update link_success_rate, clear retry timer

  on_send_failure(pkt)
    → increment retries; retry or escalate to RECOVERY

  on_neighbour_lost(node_id)
    → invalidate routes using node_id as next_hop
    → enter RECOVERY for affected destinations

  on_beacon_timer()
    → send BEACON with own mesh_id, hops_to_ground


9. Route Discovery (RREQ / RREP)
----------------------------------
1. Node A wants to send DATA to node Z. No cached route exists.
2. A floods RREQ (dst=0xFF, target=Z, origin=A, origin_seq=N, hop_count=0).
3. Each intermediate node that has not seen (A, N) rebroadcasts with
   hop_count incremented and TTL decremented.
4. Node Z (or any node with a fresh cached route to Z) sends a RREP unicast
   back toward A, using reverse path.
5. Each node on the return path caches (Z → next_hop, hop_count).
6. A receives RREP, caches route, sends DATA.

If no RREP arrives within RREQ_TIMEOUT (3 s): retry RREQ up to 2 times,
then drop the pending DATA packet (or re-queue at lower priority).


10. Artificial Topology (Indoor Testing)
-----------------------------------------
Per-node config field: allowed_neighbors: [id, id, ...]

If present, a node treats any packet from a node NOT in the list as if it
was never received — at the routing layer (not the radio driver). This forces
multi-hop paths in a lab where all nodes are physically within LoRa range.

Test topology profiles (examples you can define in config):

  line:         1—2—3—4—5—...
  sparse_mesh:  some long-range links removed to create 2–3 hop paths
  failure_test: one node's entry removed from neighbours' lists mid-run

This flag must be absent or empty for outdoor / production deployment.


11. Localisation Interface
---------------------------
The mesh core is transport-only for localisation. It does not interpret
coordinates, distances, or ranging results.

Localisation module API (what app_localise.py calls into the mesh):

  mesh.send(dst, app_id=0x01, subtype, payload_bytes)
  mesh.broadcast(app_id=0x01, subtype, payload_bytes)

Mesh delivers received localisation packets to:

  localise.on_rx(src, subtype, payload_bytes)

The localisation coordinator controls its own state machine (which node
ranges next, collecting dist reports, solving MDS, distributing results).
Only one localisation coordinator runs at a time in a local group.
Missed LoRa packets during UWB ranging turns are acceptable; retries handle
re-delivery.

Position updates are sent as per-node unicast DATA (not batched map) for v1.


12. Ground Station
-------------------
mesh_id = 99. Behaves as a normal mesh node at the network layer.
Application layer extras:

  - Accepts CTRL packets (commands out, health responses in)
  - Collects DATA from all sources for report logging
  - Does not require UWB hardware (uwb_id = null)


13. File / Module Layout
--------------------------
  main.py               startup, config load, node main loop
  node.py               mesh node: state machine, event dispatch
  packets.py            encode / decode all packet types
  neighbour_table.py    neighbour table: update, age-out, scoring
  route_table.py        route cache: lookup, insert, invalidate
  topology.py           artificial topology config loader (test-only)
  app_localise.py       localisation adapter: bridges trilat code to mesh API
  drivers/
    lora.py             SX1278 send / receive
    uwb.py              BU03 ranging interface
    thermistor.py       temperature read
  debug/
    sim.py              PC-side topology simulation (no hardware)
    test_packets.py     unit tests for packet encode/decode


14. MVP Success Criteria
--------------------------
v1 is complete when all of the following work:

  1. Neighbour discovery: eggs hear each other and build neighbour tables
  2. Forced multi-hop: indoor allowlist topology produces real 2-hop+ paths
  3. Route discovery: arbitrary egg-to-egg RREQ/RREP completes
  4. Unicast command: ground station sends a CTRL packet to a target egg
  5. Reply: target egg replies over multiple hops
  6. Local recovery: one node removed → neighbours reroute without manual reset
  7. Localisation transport: app_localise.py can send a payload from egg A
     to egg B through the mesh


15. Constants Reference
------------------------
  VERSION           = 0x01
  BROADCAST_ID      = 0xFF
  GROUND_STATION_ID = 99

  MAX_TTL           = 6
  MAX_HOP_RETRIES   = 3
  RREQ_TIMEOUT_MS   = 3000
  RREQ_MAX_ATTEMPTS = 2

  BEACON_INTERVAL_MS   = 30000
  BEACON_JITTER_MS     = 5000
  SUSPECT_TIMEOUT_MS   = 90000
  LOST_TIMEOUT_MS      = 150000

  ROUTE_CACHE_TTL_MS   = 300000
  MAX_ROUTE_FAILURES   = 3

  CMD_BUFFER_TTL_MS    = 30000

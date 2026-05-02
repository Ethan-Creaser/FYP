Restart Spec for Mayonnaise Mesh v1
==================================

Summary
-------
This document describes a minimal, decoupled restart design for the 14-node LoRa/UWB mesh (Mayonnaise) focusing on a stable mesh core with a clean interface for the localisation module.

Goals
-----
- Provide a small, testable mesh core that supports:
  - fixed node IDs
  - artificial topology allowlists for indoor multi-hop testing
  - on-demand route discovery with cached next-hops
  - neighbour lease beacons and local recovery
  - hop-by-hop ACKs and bounded retries for important packets
- Keep localisation decoupled; mesh transports localisation payloads
- No fragmentation, no deep-sleep, no security in v1

Packet Schema
-------------
All network packets use a compact binary envelope. Fields (in order):

- version (1 byte)
- kind (1 byte) : BEACON=1, RREQ=2, RREP=3, DATA=4, BCAST=5, ACK=6, RECOVERY=7
- src_id (1 byte)
- dst_id (1 byte) ; 0xFF = broadcast
- seq (2 bytes, big endian)
- ttl (1 byte)
- payload_len (1 byte)
- payload (payload_len bytes)

Application payload (inside DATA/BCAST) is type-tagged. Common pattern:

- app_id (1 byte) e.g. LOCALISE=1, THERM=2, CTRL=3
- subtype (1 byte)
- data... (remaining bytes)

Notes:
- Keep payloads under LoRa limits (~200 bytes depending on SF). No fragmentation.
- Use seq per-source to identify duplicates. Track recent (src, seq) pairs.

Neighbour Table
---------------
Entry per direct neighbour:

- node_id (1 byte)
- last_seen (ms epoch or monotonic)
- rssi (signed int8)
- snr (signed int8)
- link_success_rate (0-255)
- hops_to_ground (uint8, 0=ground)
- is_alive (bool)
- allowlisted (bool) ; computed from config

Timers / Lease
--------------
- BEACON_INTERVAL: 30s ± jitter
- SUSPECT_TIMEOUT: 90s
- LOST_TIMEOUT: 150s

Route Table
-----------
Per-destination cached route:

- dest_id (1 byte)
- next_hop (1 byte)
- hops (uint8)
- last_used (timestamp)
- failures (uint8)

Policy:
- default TTL for forwarded DATA: 6
- route cache TTL: 5 minutes
- invalidate after 3 failures or next-hop lost

State Machine (Node)
---------------------
Top-level states:
- IDLE: listening and beacons
- SENDING: actively forwarding/attempting send with retries
- RECOVERY: running local discovery for failed routes

Events:
- on_beacon_received: update neighbour table
- on_data_for_me: deliver to app layer
- on_data_to_forward: choose next hop and send
- on_ack_received: mark success
- on_send_failure: retry or mark failure
- on_neighbour_lost: trigger local recovery if routes affected

Retry & ACK
-----------
- Hop-by-hop ACK: ACK packets contain original src,dst,seq.
- Retries: up to 3 attempts per hop with small backoff.
- For important application DATA set highest queue priority.

Artificial Topology (Test-only)
--------------------------------
Add JSON config file per-node named `topology_N.json` containing allowed_neighbors: [id...].
When set, a node ignores links not in the list for routing/forwarding decisions.

Localisation Interface
----------------------
Mesh: transport-only. Localisation payloads live in DATA messages with app_id=1.

Localisation subtypes (examples):
- RANGE_CMD (0x01)
- DIST_REPORT (0x02)
- MAP_REQUEST (0x03)
- MAP_UPDATE (0x04)

Localisation should avoid blocking the mesh core; it may publish messages into the mesh core via a simple API:

mesh.send(destination, app_id=1, subtype, bytes)

Ground Station
--------------
Ground station is a normal node with a fixed ID (recommend 99). App layer treats it as the sink for logs/commands.

File Layout Proposal
--------------------
- main.py              : startup, config loader, node main loop
- node.py              : mesh node logic, state machine
- packets.py           : packet encode/decode
- neighbour_table.py   : neighbour table management
- route_table.py       : route cache
- topology.py          : artificial topology loader
- app_localise.py      : localisation app adapter (interfaces to trilat code)
- drivers/             : lora, uwb, thermistor drivers
- debug/               : test harness, simulation scripts

Next Steps
----------
1) Create minimal packet encode/decode and unit tests.
2) Implement neighbour table and BEACON sending/receiving.
3) Implement route discovery (RREQ/RREP) and next-hop caching.
4) Implement DATA send/forward with hop-by-hop ACK and retries.
5) Build topology test harness to force multi-hop indoor.

Appendix: constants
-------------------
- MAX_TTL = 6
- DEFAULT_ROUTE_TTL = 300000  # ms
- MAX_HOP_RETRIES = 3

"""Core constants for the mesh network."""

# Timing (seconds)
BEACON_INTERVAL = 30
BEACON_JITTER   = 5
SUSPECT_TIMEOUT = 90
LOST_TIMEOUT    = 150

# Routing / packets
MAX_TTL              = 6
DEFAULT_ROUTE_TTL_MS = 300_000   # 5 minutes
MAX_HOP_RETRIES      = 3
RREQ_TIMEOUT         = 3         # seconds to wait for RREP before retrying
RREQ_MAX_ATTEMPTS    = 2         # max RREQ floods before giving up on a destination

# IDs
GROUND_STATION_ID = 99
BROADCAST_ID      = 0xFF

# ── Packet kinds (4-type schema) ──────────────────────────────────────────────
#
#   BEACON  : neighbour lease beacon. TTL=1, no ACK. 1-hop only.
#   DATA    : unicast. Hop-by-hop ACK + bounded retry.
#   BCAST   : limited flood. No ACK. Duplicate suppression + TTL.
#   ACK     : hop-by-hop acknowledgement.
#
KIND_BEACON = 1
KIND_DATA   = 2
KIND_BCAST  = 3
KIND_ACK    = 4

# ── Application IDs (app_id byte, first byte of DATA/BCAST payload) ───────────
#
#   APP_ROUTING  : mesh routing control — not visible to the application layer.
#   APP_LOCALISE : UWB localisation payloads (teammate's trilat module).
#   APP_CTRL     : commands and health checks.
#   APP_THERM    : temperature telemetry (stretch goal).
#
APP_ROUTING  = 0
APP_LOCALISE = 1
APP_CTRL     = 2
APP_THERM    = 3

# ── Routing subtypes (subtype byte, used with APP_ROUTING) ────────────────────
#
#   ROUTING_RREQ     : route request — sent as BCAST flood.
#   ROUTING_RREP     : route reply   — sent as DATA unicast back to origin.
#   ROUTING_RECOVERY : topology change announcement — sent as BCAST flood.
#
ROUTING_RREQ     = 1
ROUTING_RREP     = 2
ROUTING_RECOVERY = 3

# ── APP_CTRL subtypes ─────────────────────────────────────────────────────────
CTRL_UWB_CONFIG      = 1   # payload: [uwb_id, role]
CTRL_UWB_SCAN_RESULT = 2   # payload: [uwb_id, role, slot, dist_mm_hi, dist_mm_lo, ...]
CTRL_UWB_RESTORE     = 3   # payload: [] — egg reverts to its identity.bin uwb_id, role=1

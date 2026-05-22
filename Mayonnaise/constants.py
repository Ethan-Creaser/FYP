"""Core constants for the mesh network."""

# Timing (seconds)
BEACON_INTERVAL      = 30
BEACON_INTERVAL_FAST = 10   # beacon rate for the first BEACON_FAST_DURATION seconds after boot
BEACON_FAST_DURATION = 60   # seconds to use fast beacon rate after boot
BEACON_JITTER        = 5
SUSPECT_TIMEOUT = 90
LOST_TIMEOUT    = 150

# Routing / packets
MAX_TTL              = 6
# Routes are evicted by failure (penalize) or node loss (invalidate_next_hop),
# not by time — nodes are static so cached routes stay valid indefinitely.
HOP_ACK_TIMEOUT      = 5         # seconds before retrying an unACKed hop
MAX_HOP_RETRIES      = 3
RREQ_TIMEOUT         = 6         # seconds to wait for RREP before retrying
RREQ_MAX_ATTEMPTS    = 3         # max RREQ floods before giving up on a destination

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
CTRL_UWB_CONFIG        = 1   # payload: [uwb_id, role]
CTRL_UWB_SCAN_RESULT   = 2   # payload: [uwb_id, role, slot, dist_mm_hi, dist_mm_lo, ...]
CTRL_UWB_RESTORE       = 3   # payload: [] — egg reverts to its identity.bin uwb_id, role=1
CTRL_IDENTITY_WRITE    = 4   # payload: [uwb_id, count, n0, n1, ...] — rewrite identity.bin + live allowlist
CTRL_IDENTITY_ACK      = 5   # payload: [node_id, uwb_id, count, n0, n1, ...] — confirmation sent back to requester
CTRL_BEACON            = 6   # payload: [0=disable | 1=enable] — toggle beaconing, persists to identity.bin
CTRL_PING              = 7   # payload: [] — receiver does nothing; sender uses ACK for RSSI/RTT measurement
CTRL_GET_NEIGHBOURS    = 8   # payload: [] — request alive neighbour table; egg replies with CTRL_NEIGHBOURS_REPORT
CTRL_NEIGHBOURS_REPORT = 9   # payload: [node_id, count, n0, n1, ...] — alive neighbours, sent to GROUND_STATION_ID
CTRL_GET_ROUTES        = 10  # payload: [] — request route table dump; egg replies with CTRL_ROUTES_REPORT
CTRL_ROUTES_REPORT     = 11  # payload: [node_id, count, dst_0, next_hop_0, dst_1, next_hop_1, ...] — route table
CTRL_UWB_DISABLE       = 12  # payload: [0=disable | 1=enable] — hold/release UWB reset pin to save power
CTRL_RESET_STATE       = 13  # payload: [] — synchronized state wipe for formation-time measurement
CTRL_FORMATION_REPORT  = 14  # payload: [node_id, ft_ds_hi, ft_ds_lo] — time from reset to full local connectivity (deciseconds)

# ── Packet-kind colours ───────────────────────────────────────────────────────
COLOUR_BEACON = (255, 165,   0)   # Orange
COLOUR_DATA   = (255,   0, 255)   # Magenta
COLOUR_BCAST  = (255, 255,   0)   # Yellow
COLOUR_ACK    = (  0, 255,   0)   # Green

# ── System-state colours ──────────────────────────────────────────────────────
COLOUR_IDLE   = (  0, 255, 255)   # Cyan
COLOUR_BOOT   = (255, 255, 255)   # White
COLOUR_TX     = (  0,   0, 255)   # Blue
COLOUR_RX     = (128,   0, 128)   # Purple
COLOUR_ERROR  = (255,   0,   0)   # Red

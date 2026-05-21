"""Range-test ping state machine.

Sends DATA pings one at a time, waiting for each ACK (or give-up after retries)
before firing the next.  Owned by main.py; driven from two call sites:

    ping.start(node, target_id, n_packets)   # from BLE RX handler
    ping.poll(node)                          # from main loop
"""

import time
import constants

_ROUTE_SETTLE_S = 2   # seconds to wait after route is confirmed before pinging


class PingState:
    def __init__(self):
        self._active      = False
        self._waiting     = False   # True while waiting for route discovery
        self._ready_after = 0       # monotonic time after which pings may fire
        self._target      = 0
        self._n_left      = 0
        self._n_total     = 0
        self._pending_seq = None
        self._first_seq   = None
        self._last_seq    = None

    @property
    def active(self):
        return self._active

    def start(self, node, target_id, n_packets):
        self._target      = target_id
        self._n_left      = n_packets
        self._n_total     = n_packets
        self._pending_seq = None
        self._first_seq   = None
        self._last_seq    = None
        self._active      = True

        if node.routes.get_next_hop(target_id) is not None:
            self._waiting     = False
            self._ready_after = time.time() + _ROUTE_SETTLE_S
            print("PING_START")
        else:
            self._waiting     = True
            self._ready_after = 0
            node._flood_rreq(target_id)
            print("PING_WAIT route={}".format(target_id))

    def poll(self, node):
        if not self._active:
            return

        if self._waiting:
            next_hop = node.routes.get_next_hop(self._target)
            if next_hop is not None:
                self._waiting     = False
                self._ready_after = time.time() + _ROUTE_SETTLE_S
                print("PING_ROUTE target={} next_hop={}".format(self._target, next_hop))
                print("PING_START")
            else:
                return

        if time.time() < self._ready_after:
            return

        # Clear pending_seq once the outstanding entry is gone (ACKed or gave up).
        if self._pending_seq is not None:
            if (node.node_id, self._pending_seq) not in node.outstanding:
                self._pending_seq = None
        # Fire next ping only when nothing is in flight.
        if self._pending_seq is None:
            if self._n_left > 0:
                seq = node.send_data(self._target, constants.APP_CTRL,
                                     constants.CTRL_PING, b"ping")
                self._n_left      -= 1
                self._pending_seq  = seq
                if self._first_seq is None:
                    self._first_seq = seq
                self._last_seq = seq
            else:
                self._active = False
                print("PING_DONE first_seq={} last_seq={}".format(
                    self._first_seq, self._last_seq))

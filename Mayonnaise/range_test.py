"""Range-test ping state machine.

Sends DATA pings one at a time, waiting for each ACK (or give-up after retries)
before firing the next.  Owned by main.py; driven from two call sites:

    ping.start(node.node_id, target_id, n_packets)   # from BLE RX handler
    ping.poll(node)                                   # from main loop
"""

import constants


class PingState:
    def __init__(self):
        self._active      = False
        self._target      = 0
        self._n_left      = 0
        self._n_total     = 0
        self._pending_seq = None
        self._first_seq   = None
        self._last_seq    = None

    @property
    def active(self):
        return self._active

    def start(self, node_id, target_id, n_packets):
        self._active      = True
        self._target      = target_id
        self._n_left      = n_packets
        self._n_total     = n_packets
        self._pending_seq = None
        self._first_seq   = None
        self._last_seq    = None
        print("PING_START")

    def poll(self, node):
        if not self._active:
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

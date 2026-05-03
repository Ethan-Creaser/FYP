"""OLED status helper — shows node id, test stats and link vitals.

Uses `Drivers.oled.oled_class.OLED` when available. Falls back to console printing
when running on a desktop or when the driver isn't present.
"""

try:
    from Drivers.oled.oled_class import OLED as _OLED
except Exception:
    _OLED = None

import time


class OLEDStatus:
    def __init__(self, sda=None, scl=None, width=128, height=64, freq=100000):
        self.oled = None
        try:
            if _OLED is not None:
                if sda is None and scl is None:
                    self.oled = _OLED()
                else:
                    self.oled = _OLED(sda=sda, scl=scl, width=width, height=height, freq=freq)
        except Exception:
            self.oled = None

        self.node = None
        self.success = 0
        self.fail = 0
        self.last_seq = None
        self.last_msg = ""
        self.target = None
        self.last_rssi = None
        self.last_snr = None
        self.last_rx_from = None
        self.start_time = None

    def attach_node(self, node):
        """Attach a `Node` instance so the display can show node-specific vitals."""
        self.node = node
        # prefer node's start_time if available
        self.start_time = getattr(node, "start_time", time.time())
        self._redraw()

    def update_on_send(self, seq, target):
        self.last_seq = seq
        self.target = target
        self.last_msg = "SENT"
        self._redraw()

    def update_on_ack(self, seq):
        self.success += 1
        self.last_seq = seq
        self.last_msg = "ACK"
        self._redraw()

    def update_on_timeout(self, seq):
        self.fail += 1
        self.last_seq = seq
        self.last_msg = "TIMEOUT"
        self._redraw()

    def update_on_rx(self, rssi=None, snr=None, from_id=None, src=None):
        self.last_rssi = rssi
        self.last_snr = snr
        self.last_rx_from = from_id or src
        self._redraw()

    def _format_uptime(self, seconds):
        if seconds is None:
            return "-"
        s = int(seconds)
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    def _redraw(self):
        node_id = self.node.node_id if self.node else "-"
        neigh_count = 0
        uptime = None
        if self.node:
            try:
                a = self.node.neighbours.get_alive()
                neigh_count = len(a)
            except Exception:
                neigh_count = 0
            try:
                uptime = time.time() - getattr(self.node, "start_time", self.start_time or time.time())
            except Exception:
                uptime = None

        lines = []
        lines.append(f"ID:{node_id} T:{self.target or '-'} S:{self.success} F:{self.fail}")
        lines.append(f"Last:{self.last_seq or '-'} {self.last_msg}")
        lines.append(f"RSSI:{self.last_rssi if self.last_rssi is not None else '-'} SNR:{self.last_snr if self.last_snr is not None else '-'}")
        lines.append(f"Nei:{neigh_count} Up:{self._format_uptime(uptime)}")

        if self.oled:
            try:
                self.oled.display_text('\n'.join(lines), x=0, y=0, clear=True)
            except Exception:
                print("OLED redraw failed;", lines)
        else:
            print("OLED:")
            for l in lines:
                print("  ", l)

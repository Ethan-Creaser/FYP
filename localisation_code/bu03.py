"""
bu03.py — BU03 UWB Module Driver
==================================
Handles all BU03 communication:
  - AT command configuration (role, channel, rate)
  - Hardware reset via GPIO
  - Binary frame parsing (37-byte frames, \xaa\x25\x01 header)
  - Distance scanning

Confirmed pinout (assembled PCB):
  AT commands : UART1  tx=2,  rx=1
  Data stream : UART2  tx=17, rx=18   (PA3=GPIO17, PA2=GPIO18)
  Reset pin   : GPIO15
"""

import utime
import struct
from machine import UART, Pin
from machine import reset as hard_reset

# ── Pin constants ─────────────────────────────────────────────────────────────
PIN_AT_TX   = 2
PIN_AT_RX   = 1
PIN_DAT_TX  = 17   # ESP TX → BU03 PA3
PIN_DAT_RX  = 18   # ESP RX ← BU03 PA2 (confirmed by pin scan)
PIN_RESET   = 15

# ── Frame constants ───────────────────────────────────────────────────────────
FRAME_HEADER     = b'\xaa\x25\x01'
FRAME_LEN        = 37
FRAME_TIMEOUT_MS = 1500
BOOT_HOLD_MS     = 500
BOOT_WAIT_MS     = 4000   # cold boot
BOOT_WAIT_WARM   = 2000   # warm restart


class BU03:
    """Driver for the BU03-Kit UWB module."""

    def __init__(self):
        self._rst = Pin(PIN_RESET, Pin.OUT, value=1)
        self._buf = bytearray()
        self._init_uarts()
        utime.sleep_ms(500)

    # ── UART management ───────────────────────────────────────────────────────

    def _init_uarts(self):
        """Create fresh UART objects. Must be called after every hardware reset."""
        self.at   = UART(1, baudrate=115200, tx=PIN_AT_TX,
                         rx=PIN_AT_RX,  timeout=10)
        self.data = UART(2, baudrate=115200, tx=PIN_DAT_TX,
                         rx=PIN_DAT_RX, timeout=10)

    def _reset(self, warm=False):
        """Hardware reset the BU03 and reinitialise UARTs."""
        self._rst.value(0)
        utime.sleep_ms(BOOT_HOLD_MS)
        self._rst.value(1)
        utime.sleep_ms(BOOT_WAIT_WARM if warm else BOOT_WAIT_MS)
        self._init_uarts()
        utime.sleep_ms(200)
        self._buf = bytearray()

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure(self, node_id, role, ch=1, rate=1, warm=False):
        """
        Send AT+SETCFG and AT+SAVE then hardware reset.
        role: 0 = tag, 1 = anchor
        warm: True for faster role switching in steady state
        """
        cmd = "AT+SETCFG={},{},{},{}\r\n".format(node_id, role, ch, rate)
        print("[BU03] Sending:", cmd.strip())
        self.at.write(cmd)
        utime.sleep_ms(1000)
        print("[BU03] SETCFG:", self.at.read())
        self.at.write("AT+SAVE\r\n")
        utime.sleep_ms(1000)
        print("[BU03] SAVE:", self.at.read())
        print("[BU03] Resetting ({})...".format("warm" if warm else "cold"))
        self._reset(warm=warm)
        print("[BU03] Ready — role={} id={}".format(role, node_id))

    def configure_warm(self, node_id, role, ch=1, rate=1):
        """Faster role switch for nodes already running (2s boot wait)."""
        self.configure(node_id, role, ch, rate, warm=True)

    # ── Frame reading ─────────────────────────────────────────────────────────

    def read_frame(self, timeout_ms=FRAME_TIMEOUT_MS):
        """
        Read one 37-byte UWB frame from UART2.
        Returns list of 8 distances (metres or None) or None on timeout.
        Uses header-to-header boundary detection for robustness.
        """
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if self.data.any():
                chunk = self.data.read()
                if chunk:
                    self._buf += chunk

            # Discard bytes before header
            while len(self._buf) >= 3 and self._buf[0:3] != FRAME_HEADER:
                self._buf = self._buf[1:]

            if len(self._buf) < FRAME_LEN:
                utime.sleep_ms(10)
                continue

            # Find next header to determine frame boundary
            next_hdr = None
            for k in range(3, len(self._buf) - 2):
                if self._buf[k:k+3] == FRAME_HEADER:
                    next_hdr = k
                    break

            if next_hdr is not None:
                frame_end = next_hdr
            elif self._buf[FRAME_LEN-1] == 0x55:
                frame_end = FRAME_LEN
            else:
                utime.sleep_ms(10)
                continue

            frame = bytes(self._buf[:frame_end])
            self._buf = self._buf[frame_end:]

            if len(frame) < 35:
                continue

            out = []
            for i in range(8):
                offset = 3 + i * 4
                if offset + 4 > len(frame):
                    out.append(None)
                    continue
                raw = struct.unpack('<I', frame[offset:offset+4])[0]
                out.append(float(raw) / 1000.0 if raw > 0 else None)
            return out

        return None

    # ── Scanning ──────────────────────────────────────────────────────────────

    def scan(self, n=20):
        """
        Collect n successful frames. Returns dict of slot→best_distance_m.
        Hard resets board if zero good frames (unrecoverable state).
        """
        best = {}
        good = 0
        attempts = 0
        max_attempts = n * 4

        while good < n and attempts < max_attempts:
            attempts += 1
            f = self.read_frame()
            if f is None:
                print("[BU03] timeout {}/{} buf={}b".format(
                    attempts, max_attempts, len(self._buf)))
                continue
            good += 1
            for idx, d in enumerate(f):
                if d and d > 0 and (idx not in best or d < best[idx]):
                    best[idx] = d

        print("[BU03] scan done: {}/{} good frames".format(good, n))

        if good == 0:
            print("[BU03] ZERO good frames — hard resetting")
            utime.sleep_ms(500)
            hard_reset()

        return best

    def scan_distances(self, n=20):
        """Returns sorted list of all non-zero distances in metres."""
        raw = self.scan(n)
        return sorted([d for d in raw.values() if d and d > 0])

    def scan_with_slots(self, n=5):
        """
        Quick scan for heartbeat distance reporting.
        Returns raw slot dict for laptop to process.
        """
        return self.scan(n)

    def flush(self):
        """Flush the UART receive buffer."""
        if self.data.any():
            self.data.read()
        self._buf = bytearray()

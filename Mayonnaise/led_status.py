"""NeoPixel RGB LED status indicator for the mesh node.

State machine (non-blocking):
  BOOT  -> white until set_idle() or first packet
  TX    -> COLOUR_TX (blue) for _PHASE_DIR_MS
  RX    -> COLOUR_RX (purple) for _PHASE_DIR_MS
  KIND  -> packet-kind colour for _PHASE_KIND_MS
  IDLE  -> COLOUR_IDLE (cyan)
  ERROR -> COLOUR_ERROR (red, sticky until cleared)
"""

try:
    from machine import Pin
    import neopixel
    HAVE_HW = True
except Exception:
    Pin = None
    neopixel = None
    HAVE_HW = False

try:
    import utime as _time
    def _ticks_ms():
        return _time.ticks_ms()
    def _ticks_diff(end, start):
        return _time.ticks_diff(end, start)
except Exception:
    import time as _time
    def _ticks_ms():
        return int(_time.time() * 1000)
    def _ticks_diff(end, start):
        return end - start

import constants

_PHASE_DIR_MS  = 80    # duration of TX/RX direction flash
_PHASE_KIND_MS = 250   # duration of packet-kind colour flash
_BRIGHTNESS    = 0.15  # scale factor to avoid blinding


def _scale(colour):
    return tuple(int(c * _BRIGHTNESS) for c in colour)


def _kind_to_colour(kind):
    if kind == constants.KIND_BEACON:
        return constants.COLOUR_BEACON
    if kind == constants.KIND_DATA:
        return constants.COLOUR_DATA
    if kind == constants.KIND_BCAST:
        return constants.COLOUR_BCAST
    if kind == constants.KIND_ACK:
        return constants.COLOUR_ACK
    return constants.COLOUR_IDLE


class LEDStatus:
    """Single-pixel NeoPixel status indicator."""

    def __init__(self, pin=38, num_leds=1):
        self._active = False
        if not HAVE_HW:
            return
        try:
            self._np = neopixel.NeoPixel(Pin(pin), num_leds)
            self._active = True
        except Exception as e:
            print("LEDStatus init failed:", e)
            return
        self._phase = "boot"
        self._phase_end = 0
        self._kind_colour = constants.COLOUR_IDLE
        self._set(constants.COLOUR_BOOT)

    def _set(self, colour):
        if not self._active:
            return
        try:
            self._np[0] = _scale(colour)
            self._np.write()
        except Exception:
            pass

    # ── Event hooks ───────────────────────────────────────────────────────────

    def on_tx(self, kind):
        """Call before transmitting a packet."""
        if not self._active:
            return
        if self._phase == "error":
            return
        self._kind_colour = _kind_to_colour(kind)
        self._phase = "tx"
        self._phase_end = _ticks_ms() + _PHASE_DIR_MS
        self._set(constants.COLOUR_TX)

    def on_rx(self, kind):
        """Call after receiving a valid packet."""
        if not self._active:
            return
        if self._phase == "error":
            return
        self._kind_colour = _kind_to_colour(kind)
        self._phase = "rx"
        self._phase_end = _ticks_ms() + _PHASE_DIR_MS
        self._set(constants.COLOUR_RX)

    # ── System-state helpers ──────────────────────────────────────────────────

    def set_idle(self):
        if not self._active:
            return
        self._phase = "idle"
        self._set(constants.COLOUR_IDLE)

    def set_error(self):
        if not self._active:
            return
        self._phase = "error"
        self._set(constants.COLOUR_ERROR)

    def clear_error(self):
        if not self._active:
            return
        if self._phase == "error":
            self._phase = "idle"
            self._set(constants.COLOUR_IDLE)

    # ── Main-loop poll ────────────────────────────────────────────────────────

    def poll(self):
        """Advance the LED state machine. Call every main-loop iteration."""
        if not self._active:
            return
        if self._phase not in ("tx", "rx", "kind"):
            return
        now = _ticks_ms()
        if self._phase in ("tx", "rx"):
            if _ticks_diff(self._phase_end, now) <= 0:
                self._phase = "kind"
                self._phase_end = now + _PHASE_KIND_MS
                self._set(self._kind_colour)
        elif self._phase == "kind":
            if _ticks_diff(self._phase_end, now) <= 0:
                self._phase = "idle"
                self._set(constants.COLOUR_IDLE)

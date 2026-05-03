"""Hardware radio adapter for the mesh core.

This adapter is intentionally minimal: it exposes `send(bytes)` and `poll(timeout_ms)`.
It wraps the existing MicroPython `Drivers.lora.lora.ULoRa` class when running on-device.

Usage (device):
  radio = HardwareRadio(node, config)
  # either call poll() regularly in your main loop, or call start_background()
  radio.poll()

Note: this file will raise at import if MicroPython `machine` and driver modules are missing.
On desktop simulation `node` should not call this adapter.
"""

# Avoid `typing` imports for MicroPython compatibility

try:
    # MicroPython-specific imports; will fail on desktop
    from Drivers.lora.lora import ULoRa
    from machine import SPI, Pin
    import _thread
    HAVE_HW = True
except Exception:
    ULoRa = None
    SPI = None
    Pin = None
    _thread = None
    HAVE_HW = False


class HardwareRadio:
    def __init__(self, node, config: dict):
        if not HAVE_HW:
            raise RuntimeError("Hardware radio not available in this runtime")
        self.node = node
        self.config = config
        lconf = config.get("lora", {})
        pins = config.get("lora_pins", {})
        spi_id = pins.get("spi_id", 1)
        # Build SPI and pin objects (MicroPython)
        spi = SPI(spi_id, baudrate=5000000, polarity=0, phase=0,
                  sck=Pin(pins.get("sck", 12)), mosi=Pin(pins.get("mosi", 11)), miso=Pin(pins.get("miso", 13)))
        pins_map = {"ss": pins.get("ss", 10), "reset": pins.get("reset", 4), "dio0": pins.get("dio0", 5)}
        params = {
            "frequency": lconf.get("frequency", 433000000),
            "tx_power_level": lconf.get("tx_power", 10),
            "signal_bandwidth": lconf.get("bandwidth", 125000),
            "spreading_factor": lconf.get("spreading_factor", 9),
        }
        self.lora = ULoRa(spi, pins_map, parameters=params)
        self._bg_running = False
        # LBT/CAD parameters (tunable via config.json -> "lbt" block)
        lbt_cfg = config.get("lbt", {})
        self.lbt_enabled = bool(lbt_cfg.get("enabled", True))
        self.cad_timeout_ms = int(lbt_cfg.get("cad_timeout_ms", 50))
        self.lbt_attempts = int(lbt_cfg.get("attempts", 3))
        self.backoff_min_ms = int(lbt_cfg.get("backoff_min_ms", 20))
        self.backoff_max_ms = int(lbt_cfg.get("backoff_max_ms", 220))

    def send(self, data: bytes):
        # transmit raw bytes
        try:
            print(f"[radio] TX len={len(data)}")
        except Exception:
            pass
        # Listen-before-talk: use CAD (channel activity detection) if enabled and available
        try:
            if self.lbt_enabled and hasattr(self.lora, "channel_active"):
                attempts = max(1, int(self.lbt_attempts))
                for attempt in range(attempts):
                    try:
                        busy = self.lora.channel_active(timeout_ms=self.cad_timeout_ms)
                    except Exception:
                        busy = False
                    if busy:
                        try:
                            print(f"[radio] CAD busy, backoff {attempt+1}/{attempts}")
                        except Exception:
                            pass
                        # pick random backoff between configured min/max
                        try:
                            import random as _rand
                            backoff_ms = int(_rand.random() * (self.backoff_max_ms - self.backoff_min_ms)) + self.backoff_min_ms
                        except Exception:
                            try:
                                import urandom as _ur
                                backoff_ms = (_ur.getrandbits(16) % (self.backoff_max_ms - self.backoff_min_ms)) + self.backoff_min_ms
                            except Exception:
                                try:
                                    import utime as _ut
                                    backoff_ms = (_ut.ticks_ms() % (self.backoff_max_ms - self.backoff_min_ms)) + self.backoff_min_ms
                                except Exception:
                                    backoff_ms = (self.backoff_min_ms + self.backoff_max_ms) // 2
                        # sleep for backoff_ms if possible
                        try:
                            import utime as _ut
                            _ut.sleep_ms(backoff_ms)
                        except Exception:
                            try:
                                import time as _t
                                _t.sleep(backoff_ms / 1000.0)
                            except Exception:
                                pass
                        # try CAD again
                        continue
                    # channel clear, proceed to send
                    break
        except Exception:
            # if CAD check fails, continue with blind send
            pass

        self.lora.begin_packet()
        self.lora.write(data)
        self.lora.end_packet()

    def poll(self, timeout_ms: int = 500):
        # blocking listen for up to timeout_ms; if payload found, deliver to node
        payload = self.lora.listen(timeout=timeout_ms)
        if payload:
            try:
                rssi = self.lora.packet_rssi()
            except Exception:
                rssi = None
            try:
                snr = self.lora.packet_snr()
            except Exception:
                snr = None
            try:
                print(f"[radio] RX len={len(payload)} rssi={rssi} snr={snr}")
            except Exception:
                pass
            # from_id isn't known at radio-level; node will parse pkt.src
            self.node.receive_raw(payload, from_id=None, rssi=rssi, snr=snr)

    def start_background(self, timeout_ms: int = 500):
        if _thread is None:
            raise RuntimeError("_thread not available on this build")
        if self._bg_running:
            return
        self._bg_running = True

        def _loop():
            while self._bg_running:
                try:
                    self.poll(timeout_ms=timeout_ms)
                except Exception:
                    # tolerate errors and continue
                    pass

        _thread.start_new_thread(_loop, ())

    def stop_background(self):
        self._bg_running = False

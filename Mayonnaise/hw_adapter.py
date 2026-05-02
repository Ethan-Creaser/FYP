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

from typing import Optional

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

    def send(self, data: bytes):
        # transmit raw bytes
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

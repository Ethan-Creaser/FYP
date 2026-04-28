"""
comms.py — LoRa Communication Driver
======================================
Wraps the LoRaTransceiver to provide simple send/receive
with JSON message encoding and decoding.

Uses listen(timeout=50) — the only correct way to use this driver
since received_packet() has a mode-switching bug.

Pinout:
  NSS=10, SCK=12, MOSI=11, MISO=13, RESET=4, DIO0=5
"""

import ujson
import utime
from machine import Pin, SPI
from Drivers.lora.transceiver import LoRaTransceiver

PIN_NSS   = 10
PIN_SCK   = 12
PIN_MOSI  = 11
PIN_MISO  = 13
PIN_RESET = 4
PIN_DIO0  = 5


class Comms:
    """LoRa communication wrapper."""

    def __init__(self):
        spi = SPI(1, baudrate=5_000_000, polarity=0, phase=0,
                  sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI), miso=Pin(PIN_MISO))
        self._radio = LoRaTransceiver(spi=spi, pins={
            "ss":    PIN_NSS,
            "reset": PIN_RESET,
            "dio0":  PIN_DIO0,
        })
        print("[Comms] LoRa OK")

    def send(self, obj):
        """Send a dict as JSON over LoRa."""
        payload = ujson.dumps(obj).encode()
        self._radio.send(payload)

    def recv(self, timeout_ms=50):
        """
        Non-blocking receive. Returns decoded dict or None.
        timeout_ms: how long listen() waits internally.
        """
        raw = self._radio.lora.listen(timeout=timeout_ms)
        if not raw:
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode()
            return ujson.loads(raw)
        except Exception:
            print("[Comms] decode failed:", raw)
            return None

    def rssi(self):
        return self._radio.lora.packet_rssi()

    def snr(self):
        return self._radio.lora.packet_snr()

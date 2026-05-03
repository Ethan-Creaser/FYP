"""
Two-Way Ranging (TWR) over LoRa — estimates distance from round-trip timing.

Protocol
--------
  INITIATOR                           RESPONDER
  load PING into FIFO
  set DIO0 → TxDone mapping
  set TX mode ──── PING ─────────>    DIO0 IRQ fires → t2_us (RxDone)
  DIO0 IRQ fires → t1_us (TxDone)       load PONG into FIFO
  set DIO0 → RxDone mapping             set DIO0 → TxDone mapping
  set RX mode                           set TX mode ──── PONG ─────>
  DIO0 IRQ fires → t4_us (RxDone)    DIO0 IRQ fires → t3_us (TxDone)

  RTT = t4_us - t1_us          (both on initiator clock)
  ToF = (RTT - pong_airtime_us) / 2
  dist = ToF × 1e-6 × 3×10⁸   [metres]

  pong_airtime accounts for the frame being in the air between t3 and t4.
  A small residual (ISR entry latency, ~1–5 µs) can be zeroed with
  CALIBRATION_US measured at a known reference distance.

Precision
---------
  Timestamps captured in the DIO0 hardware ISR at the TX_DONE / RX_DONE
  edge, not in a software poll loop.  ISR entry latency in MicroPython is
  typically 1–10 µs, giving a practical floor of ~1.5–3 km.  This is still
  LoRa — useful for multi-km coarse ranging only.

Usage
-----
  Set ROLE = "initiator" on one device and "responder" on the other.
  Both must share the same LORA_PARAMS.
"""

import utime
import machine
from machine import Pin, SPI
from Drivers.lora.lora import (
    ULoRa,
    REG_OP_MODE, REG_IRQ_FLAGS, REG_DIO_MAPPING_1,
    MODE_LONG_RANGE_MODE, MODE_TX, MODE_RX_CONTINUOUS,
    IRQ_TX_DONE_MASK, IRQ_RX_DONE_MASK, IRQ_PAYLOAD_CRC_ERROR_MASK,
)

# ── Configuration ────────────────────────────────────────────────────────────

ROLE = "initiator"   # "initiator" | "responder"

LORA_PARAMS = {
    "frequency":        433_000_000,
    "spreading_factor": 9,
    "bandwidth":        125000,
    "coding_rate":      5,      # 4/5
    "preamble_length":  8,
    "output_power":     10,
    "crc":              True,
}

N_PINGS          = 20          # ranging exchanges (initiator only)
PING_INTERVAL_MS = 500
RX_TIMEOUT_US    = 2_000_000   # 2 s

# Zero out residual ISR-entry latency.  Measure at a known reference distance:
#   CALIBRATION_US = measured_ToF_us - (actual_distance_m / 300)
# Set to 0 until you have a reference measurement.
CALIBRATION_US = 0

# ── DIO0 GPIO polling ────────────────────────────────────────────────────────
# Poll the DIO0 pin directly instead of going through SPI or using soft IRQ.
# GPIO reads are ~1-5 µs per iteration vs ~50-200 µs for SPI register reads,
# and avoid soft-IRQ scheduling latency entirely.

_dio0_pin = None   # set by _make_lora


def _wait_dio0(timeout_us=2_000_000):
    """
    Spin until DIO0 goes high.  Returns ticks_us() at the rising edge, or None.

    machine.disable_irq() wraps each (pin-read + ticks_us) pair so FreeRTOS
    cannot context-switch between sampling the GPIO and recording the time.
    Interrupts are re-enabled immediately after each iteration, so the
    worst-case interrupt-disabled window is one tight loop body (~few µs).
    """
    deadline = utime.ticks_add(utime.ticks_us(), timeout_us)
    pin = _dio0_pin   # local lookup is faster inside the loop
    while True:
        state = machine.disable_irq()
        if pin.value():
            t = utime.ticks_us()
            machine.enable_irq(state)
            return t
        machine.enable_irq(state)
        if utime.ticks_diff(deadline, utime.ticks_us()) <= 0:
            return None

# ── Low-level timed TX / RX ───────────────────────────────────────────────────

# DIO0 mapping bits [7:6] in REG_DIO_MAPPING_1
_DIO0_RXDONE = 0x00
_DIO0_TXDONE = 0x40


def _tx_timed(lora):
    """
    Transmit whatever is already loaded in the FIFO.
    Returns ticks_us() sampled as soon as DIO0 goes high (TX_DONE), or None.
    """
    lora.write_register(REG_DIO_MAPPING_1,
                        (lora.read_register(REG_DIO_MAPPING_1) & 0x3F) | _DIO0_TXDONE)
    lora.write_register(REG_IRQ_FLAGS, 0xFF)           # clear stale flags → DIO0 low
    lora.write_register(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_TX)
    t = _wait_dio0()                                   # spin on GPIO until TX_DONE
    lora.write_register(REG_IRQ_FLAGS, IRQ_TX_DONE_MASK)
    return t


def _rx_timed(lora, timeout_us=2_000_000):
    """
    Arm continuous RX.  Blocks until a packet arrives or timeout.
    Returns (ticks_us at RX_DONE, payload_bytes) or (None, None).
    """
    lora.write_register(REG_DIO_MAPPING_1,
                        (lora.read_register(REG_DIO_MAPPING_1) & 0x3F) | _DIO0_RXDONE)
    lora.write_register(REG_IRQ_FLAGS, 0xFF)           # clear stale flags → DIO0 low
    lora.write_register(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_RX_CONTINUOUS)

    t = _wait_dio0(timeout_us)                         # spin on GPIO until RX_DONE
    if t is None:
        return None, None

    irq = lora.read_register(REG_IRQ_FLAGS)
    lora.write_register(REG_IRQ_FLAGS, 0xFF)

    if irq & IRQ_PAYLOAD_CRC_ERROR_MASK:
        return None, None

    payload = lora.read_payload()
    return t, payload

# ── Airtime calculation ───────────────────────────────────────────────────────

def _airtime_us(payload_bytes, sf, bw_hz, cr, preamble, crc):
    """SX1276 datasheet §4.1.1.6 packet airtime (µs), explicit header, no IQ invert."""
    t_sym_us = (2 ** sf) / bw_hz * 1e6
    ldr  = 1 if t_sym_us > 16000 else 0
    crc_n = 1 if crc else 0
    num  = 8 * payload_bytes - 4 * sf + 28 + 16 * crc_n
    den  = 4 * (sf - 2 * ldr)
    n_payload = 8 + max(0, -(-num // den)) * (cr + 4)
    return (preamble + 4.25 + n_payload) * t_sym_us

# ── Hardware init ─────────────────────────────────────────────────────────────

_PING = b"\x50"
_PONG = b"\x51"

def _make_lora():
    global _dio0_pin
    spi = SPI(1, baudrate=5_000_000, polarity=0, phase=0,
              sck=Pin(12), mosi=Pin(11), miso=Pin(13))
    lora = ULoRa(spi, {"ss": 10, "reset": 4, "dio0": 5}, LORA_PARAMS)
    _dio0_pin = Pin(5, Pin.IN)
    return lora

# ── TWR roles ─────────────────────────────────────────────────────────────────

def run_initiator(lora):
    p = LORA_PARAMS
    pong_air = _airtime_us(len(_PONG), p["spreading_factor"], int(p["bandwidth"]),
                           p["coding_rate"], p["preamble_length"], p["crc"])
    print("PONG airtime: {:.0f} µs  |  calibration offset: {} µs".format(
        pong_air, CALIBRATION_US))
    print("SF{}  BW{}k  CR4/{}\n".format(
        p["spreading_factor"], p["bandwidth"] // 1000, p["coding_rate"]))

    distances = []

    for i in range(N_PINGS):
        # TX PING ─────────────────────────────────────────────────────────────
        lora.begin_packet()
        lora.write(_PING)
        t1_us = _tx_timed(lora)

        if t1_us is None:
            print("[{:>2}]  TX timeout".format(i))
            utime.sleep_ms(PING_INTERVAL_MS)
            continue

        # RX PONG ─────────────────────────────────────────────────────────────
        t4_us, payload = _rx_timed(lora, RX_TIMEOUT_US)

        if t4_us is None:
            print("[{:>2}]  RX timeout / CRC error".format(i))
            utime.sleep_ms(PING_INTERVAL_MS)
            continue

        if payload != _PONG:
            print("[{:>2}]  unexpected payload: {}".format(i, payload))
            utime.sleep_ms(PING_INTERVAL_MS)
            continue

        rssi = lora.packet_rssi()
        snr  = lora.packet_snr()

        # Distance ────────────────────────────────────────────────────────────
        rtt_us = utime.ticks_diff(t4_us, t1_us)
        tof_us = (rtt_us - pong_air - CALIBRATION_US) / 2.0
        dist_m = tof_us * 1e-6 * 3e8

        print("[{:>2}]  RTT={:>7} µs  ToF={:>7.1f} µs  dist={:>8.0f} m"
              "  RSSI={:>4} dBm  SNR={:>5.1f} dB".format(
              i, rtt_us, tof_us, dist_m, rssi, snr))

        distances.append(dist_m)
        utime.sleep_ms(PING_INTERVAL_MS)

    if distances:
        avg = sum(distances) / len(distances)
        mn  = min(distances)
        mx  = max(distances)
        print("\n--- {} measurements ---".format(len(distances)))
        print("  mean  : {:.0f} m".format(avg))
        print("  min   : {:.0f} m".format(mn))
        print("  max   : {:.0f} m".format(mx))
        print("  spread: {:.0f} m  ({:.1f} µs jitter)".format(
            mx - mn, (mx - mn) / 300))
        print("\nTo calibrate: set CALIBRATION_US = {:.0f}  (measured at {:.0f} m)".format(
            2 * distances[0] / 300 + CALIBRATION_US,   # first sample as ref
            distances[0]))


def run_responder(lora):
    print("Responder ready (IRQ mode)...")

    while True:
        t2_us, payload = _rx_timed(lora, timeout_us=30_000_000)  # 30 s idle timeout

        if t2_us is None:
            print("idle...")
            continue

        if payload != _PING:
            continue

        # TX PONG as fast as possible — minimise turnaround
        lora.begin_packet()
        lora.write(_PONG)
        _tx_timed(lora)
        print("PONG sent  RSSI={} dBm".format(lora.packet_rssi()))

# ── Entry point ───────────────────────────────────────────────────────────────

lora = _make_lora()

if ROLE == "initiator":
    run_initiator(lora)
elif ROLE == "responder":
    run_responder(lora)
else:
    raise ValueError("ROLE must be 'initiator' or 'responder'")

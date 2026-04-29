import struct
import utime

from machine import Pin, UART


FRAME_HEADER = b"\xaa\x25\x01"
FRAME_LEN = 37
RESET_HOLD_MS = 500
BOOT_WAIT_COLD_MS = 4000
BOOT_WAIT_WARM_MS = 2000


class BU03:
    """
    BU03 driver with explicit config/data UARTs and buffered frame parsing.

    Role meanings:
      0 = tag
      1 = anchor/base
    """

    def __init__(
        self,
        data_uart_id=1,
        data_tx=17,
        data_rx=18,
        config_uart_id=2,
        config_tx=2,
        config_rx=1,
        reset_pin=15,
    ):
        self.data_uart_id = data_uart_id
        self.data_tx = data_tx
        self.data_rx = data_rx
        self.config_uart_id = config_uart_id
        self.config_tx = config_tx
        self.config_rx = config_rx
        self.reset_pin = Pin(reset_pin, Pin.OUT, value=1)
        self._buffer = bytearray()
        self._init_uarts()
        utime.sleep_ms(500)

    def _init_uarts(self):
        self.data_uart = UART(
            self.data_uart_id,
            baudrate=115200,
            tx=self.data_tx,
            rx=self.data_rx,
            timeout=10,
        )
        self.config_uart = UART(
            self.config_uart_id,
            baudrate=115200,
            tx=self.config_tx,
            rx=self.config_rx,
            timeout=10,
        )

    def _reset(self, warm=False):
        self.reset_pin.value(0)
        utime.sleep_ms(RESET_HOLD_MS)
        self.reset_pin.value(1)
        utime.sleep_ms(BOOT_WAIT_WARM_MS if warm else BOOT_WAIT_COLD_MS)
        self._init_uarts()
        self._buffer = bytearray()

    def _send_at(self, command, delay_ms=1000):
        self.config_uart.write(command + "\r\n")
        utime.sleep_ms(delay_ms)
        if self.config_uart.any():
            response = self.config_uart.read()
            print(response)

    def configure(self, node_id, role, channel=1, rate=1, warm=False):
        self._send_at("AT+SETCFG={},{},{},{}".format(node_id, role, channel, rate))
        self._send_at("AT+SAVE")
        self._send_at("AT+GETCFG")
        self._reset(warm=warm)

    def configure_warm(self, node_id, role, channel=1, rate=1):
        self.configure(node_id, role, channel=channel, rate=rate, warm=True)

    def reconfigure(self, node_id, role, channel=1, rate=1):
        self.configure(node_id, role, channel=channel, rate=rate, warm=False)

    def flush(self):
        if self.data_uart.any():
            self.data_uart.read()
        self._buffer = bytearray()

    def decode_uwb_distances(self, data):
        if data is None or len(data) < 35:
            return None
        if data[0:3] != FRAME_HEADER:
            return None

        distances = []
        for index in range(8):
            offset = 3 + (index * 4)
            if offset + 4 > len(data):
                distances.append(None)
                continue
            raw = struct.unpack("<I", data[offset:offset + 4])[0]
            distances.append((raw / 1000.0) if raw > 0 else None)
        return distances

    def read_frame(self, timeout_ms=1500):
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if self.data_uart.any():
                chunk = self.data_uart.read()
                if chunk:
                    self._buffer.extend(chunk)

            while len(self._buffer) >= 3 and self._buffer[0:3] != FRAME_HEADER:
                self._buffer = self._buffer[1:]

            if len(self._buffer) < FRAME_LEN:
                utime.sleep_ms(10)
                continue

            next_header = None
            for index in range(3, len(self._buffer) - 2):
                if self._buffer[index:index + 3] == FRAME_HEADER:
                    next_header = index
                    break

            if next_header is not None:
                frame_end = next_header
            elif self._buffer[FRAME_LEN - 1] == 0x55:
                frame_end = FRAME_LEN
            else:
                utime.sleep_ms(10)
                continue

            frame = bytes(self._buffer[:frame_end])
            self._buffer = self._buffer[frame_end:]
            decoded = self.decode_uwb_distances(frame)
            if decoded is not None:
                return decoded

        return None

    def read_distance(self, timeout_ms=1500):
        return self.read_frame(timeout_ms=timeout_ms)

    def scan(self, frames=20, timeout_ms=1500):
        best = {}
        good_frames = 0
        attempts = 0
        max_attempts = max(frames * 4, frames + 1)

        while good_frames < frames and attempts < max_attempts:
            attempts += 1
            decoded = self.read_frame(timeout_ms=timeout_ms)
            if decoded is None:
                continue

            good_frames += 1
            for slot, distance in enumerate(decoded):
                if distance is None or distance <= 0:
                    continue
                current = best.get(slot)
                if current is None or distance < current:
                    best[slot] = distance

        print("[BU03] scan {} good frames".format(good_frames))
        return best

    def scan_distances(self, frames=20, timeout_ms=1500):
        raw = self.scan(frames=frames, timeout_ms=timeout_ms)
        return sorted([distance for distance in raw.values() if distance and distance > 0])

    def scan_with_slots(self, frames=5, timeout_ms=1500):
        return self.scan(frames=frames, timeout_ms=timeout_ms)

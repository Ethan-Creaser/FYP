import urandom
import utime
from machine import Pin, SPI
from utime import sleep_ms
from Drivers.lora.lora import ULoRa

_IMG_HEADER    = const(3)   # len(b"IMG")
_ACK_HEADER    = const(3)   # len(b"ACK")
_IMG_META      = const(2)   # chunk_idx + total_chunks
_IMG_CHUNK     = const(240) # image bytes per packet (keeps total <=245 < 255)


class LoRaTransceiver:
    """
    A LoRa transceiver that can both send and receive packets using ULoRa.
    The device switches between TX and RX modes as needed.
    """

    def __init__(self, spi=None, pins=None, parameters=None):
        """
        :param spi: Initialized SPI object. If None, a default SPI bus is created.
        :param pins: Dict with pin mappings: {"ss": <n>, "reset": <n>, "dio0": <n>}.
                     If None, defaults are used.
        :param parameters: Optional dict of LoRa configuration parameters.
        """
        if spi is None:
            spi = SPI(1, baudrate=5000000, polarity=0, phase=0,
                      sck=Pin(12), mosi=Pin(11), miso=Pin(13))

        if pins is None:
            pins = {
                "ss": 10,
                "reset": 4,
                "dio0": 5,
            }

        self.lora = ULoRa(spi, pins, parameters)
        self.last_rssi = None
        self.last_snr = None
        self.start_receive()

    def start_receive(self):
        """Keep the radio in continuous receive mode for polling loops."""
        self.lora.receive()

    def send(self, message, max_retries=5):
        """
        Transmit a message string or bytes.

        Performs CSMA/CA using CAD before transmitting: if the channel is busy,
        backs off by a random 20–200 ms and retries up to max_retries times.
        Transmits unconditionally on the final attempt to prevent starvation.

        :param message: str or bytes to send.
        :param max_retries: Number of CAD checks before forcing the transmit.
        """
        if isinstance(message, str):
            message = message.encode()
        for attempt in range(max_retries):
            if not self.lora.channel_active():
                break
            backoff = 20 + urandom.randint(0, 180)
            print("CAD: channel busy (attempt {}), backing off {}ms".format(attempt + 1, backoff))
            sleep_ms(backoff)
        self.lora.println(message)
        self.start_receive()
        print("Sent: {}".format(message))

    def poll_receive(self):
        """
        Return one received packet if available, without blocking the loop.

        The latest RSSI/SNR are stored on last_rssi and last_snr so the node
        can update neighbour health without changing the older receive API.
        """
        if not self.lora.received_packet():
            return None

        payload = self.lora.read_payload()
        self.last_rssi = self.lora.packet_rssi()
        self.last_snr = self.lora.packet_snr()

        if payload:
            print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(
                payload, self.last_rssi, self.last_snr
            ))
            try:
                return payload.decode()
            except Exception:
                return payload
        return None

    def receive(self, timeout=5000):
        """
        Listen for an incoming packet.

        :param timeout: Timeout in milliseconds (default 5000 ms).
        :return: Decoded string payload, or None if nothing received.
        """
        payload = self.lora.listen(timeout=timeout)
        if payload:
            self.last_rssi = self.lora.packet_rssi()
            self.last_snr = self.lora.packet_snr()
            print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(
                payload, self.last_rssi, self.last_snr
            ))
            try:
                return payload.decode()
            except Exception:
                return payload
        return None

    def send_and_wait(self, message, timeout=5000):
        """
        Send a message then immediately listen for a reply.

        :param message: str or bytes to send.
        :param timeout: Timeout in ms to wait for a reply.
        :return: Reply payload string, or None if no reply.
        """
        self.send(message)
        return self.receive(timeout=timeout)

    def send_image(self, path, ack_timeout=3000, max_retries=5, progress_cb=None):
        """
        Send a binary image file in 240-byte chunks with per-chunk ACKs.

        Packet format: b"IMG" + chunk_idx (1B) + total_chunks (1B) + data
        ACK format:    b"ACK" + chunk_idx (1B)

        :param path: Path to the .bin file on the local filesystem.
        :param ack_timeout: Milliseconds to wait for an ACK before retrying.
        :param max_retries: Attempts per chunk before giving up.
        :return: True on success, False if any chunk fails after all retries.
        """
        with open(path, "rb") as f:
            data = f.read()

        total = len(data)
        total_chunks = (total + _IMG_CHUNK - 1) // _IMG_CHUNK
        print("Sending '{}' ({} bytes, {} chunks)".format(path, total, total_chunks))

        for idx in range(total_chunks):
            chunk = data[idx * _IMG_CHUNK:(idx + 1) * _IMG_CHUNK]
            packet = b"IMG" + bytes([idx, total_chunks]) + chunk
            acked = False

            for attempt in range(1, max_retries + 1):
                self.send(packet)
                print("  chunk {}/{} sent (attempt {})".format(idx + 1, total_chunks, attempt))

                start = utime.ticks_ms()
                while utime.ticks_diff(utime.ticks_ms(), start) < ack_timeout:
                    reply = self.poll_receive()
                    if reply is not None:
                        raw = reply.encode() if isinstance(reply, str) else bytes(reply)
                        if len(raw) >= 4 and raw[:3] == b"ACK" and raw[3] == idx:
                            print("  ACK received for chunk {}".format(idx))
                            acked = True
                            break
                    sleep_ms(10)

                if acked:
                    break
                print("  timeout, retrying chunk {}...".format(idx))

            if not acked:
                print("ERROR: chunk {} failed after {} attempts.".format(idx, max_retries))
                if progress_cb:
                    progress_cb(idx, total_chunks, failed=True)
                return False

            if progress_cb:
                progress_cb(idx + 1, total_chunks, failed=False)
            sleep_ms(50)

        print("Transfer complete: {} bytes in {} chunks.".format(total, total_chunks))
        return True

    def receive_image(self, output_path="received_image.bin",
                      listen_timeout=8000, idle_timeout=10000, progress_cb=None):
        """
        Receive a chunked binary image and write it to a file.

        Packet format: b"IMG" + chunk_idx (1B) + total_chunks (1B) + data
        ACK format:    b"ACK" + chunk_idx (1B)

        :param output_path: Destination file path for the assembled image.
        :param listen_timeout: ms to wait for the very first packet.
        :param idle_timeout: ms of silence after which a stalled transfer fails.
        :return: output_path on success, None on timeout or stall.
        """
        print("Waiting for image transfer...")
        self.send(b"RDY", max_retries=2)
        print("RDY sent to sender.")
        chunks = {}
        total_chunks = None
        last_ms = utime.ticks_ms()
        started = False

        while True:
            raw = self.poll_receive()

            if raw is not None:
                data = raw.encode() if isinstance(raw, str) else bytes(raw)

                if len(data) >= _IMG_HEADER + _IMG_META and data[:3] == b"IMG":
                    chunk_idx    = data[3]
                    n_chunks     = data[4]
                    payload      = data[5:]

                    if total_chunks is None:
                        total_chunks = n_chunks
                        print("Transfer started: {} chunks expected.".format(total_chunks))
                        started = True

                    if chunk_idx not in chunks:
                        chunks[chunk_idx] = payload
                        print("  received chunk {}/{} ({} bytes)".format(
                            chunk_idx + 1, total_chunks, len(payload)))
                        if progress_cb:
                            progress_cb(len(chunks), total_chunks)

                    self.send(b"ACK" + bytes([chunk_idx]))
                    last_ms = utime.ticks_ms()

                    if len(chunks) == total_chunks:
                        with open(output_path, "wb") as f:
                            for i in range(total_chunks):
                                f.write(chunks[i])
                        total_bytes = sum(len(v) for v in chunks.values())
                        print("Saved {} bytes to '{}'.".format(total_bytes, output_path))
                        return output_path

            elapsed = utime.ticks_diff(utime.ticks_ms(), last_ms)
            if started and elapsed > idle_timeout:
                assert total_chunks is not None
                missing = [i for i in range(total_chunks) if i not in chunks]
                print("Stalled — missing chunks: {}".format(missing))
                return None
            if not started and elapsed > listen_timeout:
                print("No transfer started within timeout.")
                return None

            sleep_ms(5)


# ============================================================================
# Example usage
# ============================================================================
if __name__ == "__main__":
    import utime

    transceiver = LoRaTransceiver()
    counter = 0

    while True:
        # Send a message
        transceiver.send("Ping {}".format(counter))
        counter += 1

        # Listen for a reply
        reply = transceiver.receive(timeout=1000)
        if reply:
            print("Got reply: {}".format(reply))
        else:
            print("No reply received.")

        utime.sleep_ms(500)

# lora_image_receiver.py
#
# Receives a chunked .bin image over LoRa and saves it to disk.
# Optionally displays it on the OLED once the transfer is complete.
#
# Protocol:
#   Incoming packet: b"IMG" + chunk_idx (1B) + total_chunks (1B) + data
#   Reply:           b"ACK" + chunk_idx (1B)

import utime
from Drivers.lora.transceiver import LoRaTransceiver

LISTEN_TIMEOUT = 60000  # ms to wait for the first packet before giving up
IDLE_TIMEOUT   = 10000  # ms of silence after which transfer is considered stalled

OUTPUT_FILE    = "received_image.bin"
DISPLAY_ON_DONE = True  # set False to skip OLED display after receiving


def receive_image():
    lora = LoRaTransceiver()
    print("Waiting for image transfer...")

    chunks = {}
    total_chunks = None
    last_packet_ms = utime.ticks_ms()
    started = False

    while True:
        raw = lora.poll_receive()

        if raw is not None:
            data = raw if isinstance(raw, bytes) else raw.encode()

            if len(data) >= 5 and data[:3] == b"IMG":
                chunk_idx   = data[3]
                n_chunks    = data[4]
                payload     = data[5:]

                if total_chunks is None:
                    total_chunks = n_chunks
                    print("Transfer started: {} chunks expected.".format(total_chunks))
                    started = True

                if chunk_idx not in chunks:
                    chunks[chunk_idx] = payload
                    print("  received chunk {}/{} ({} bytes)".format(
                        chunk_idx + 1, total_chunks, len(payload)))

                # ACK this chunk regardless (handles duplicate sends)
                lora.send(b"ACK" + bytes([chunk_idx]))
                last_packet_ms = utime.ticks_ms()

                if len(chunks) == total_chunks:
                    print("All chunks received, writing '{}'...".format(OUTPUT_FILE))
                    _write_image(chunks, total_chunks)
                    if DISPLAY_ON_DONE:
                        _display(OUTPUT_FILE)
                    return True

        # Timeout checks
        elapsed = utime.ticks_diff(utime.ticks_ms(), last_packet_ms)
        if started and elapsed > IDLE_TIMEOUT:
            missing = [i for i in range(total_chunks) if i not in chunks]
            print("Stalled waiting for chunks: {}".format(missing))
            return False
        if not started and elapsed > LISTEN_TIMEOUT:
            print("No transfer started within timeout.")
            return False

        utime.sleep_ms(5)


def _write_image(chunks, total_chunks):
    with open(OUTPUT_FILE, "wb") as f:
        for i in range(total_chunks):
            f.write(chunks[i])
    print("Saved {} bytes to '{}'.".format(
        sum(len(v) for v in chunks.values()), OUTPUT_FILE))


def _display(path):
    try:
        from Drivers.oled.oled_class import OLED
        oled = OLED()
        oled.display_image(path)
        print("Image displayed on OLED.")
    except Exception as e:
        print("OLED display failed: {}".format(e))


if __name__ == "__main__":
    receive_image()

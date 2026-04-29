# lora_image_sender.py
#
# Sends a .bin image over LoRa using a simple chunked protocol.
#
# Protocol:
#   Each packet: b"IMG" + chunk_idx (1B) + total_chunks (1B) + data (<=240B)
#   Receiver replies: b"ACK" + chunk_idx (1B)
#
# Usage: run this on the transmitting node with the .bin file present.

import utime
from Drivers.lora.transceiver import LoRaTransceiver

CHUNK_SIZE = 240    # bytes of image data per LoRa packet (255 max - 5B header)
ACK_TIMEOUT = 3000  # ms to wait for ACK per chunk
MAX_RETRIES = 5     # retransmit attempts before giving up on a chunk


def send_image(path="image.bin"):
    lora = LoRaTransceiver()

    with open(path, "rb") as f:
        data = f.read()

    total = len(data)
    total_chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    print("Sending '{}' ({} bytes, {} chunks)".format(path, total, total_chunks))

    for idx in range(total_chunks):
        chunk = data[idx * CHUNK_SIZE:(idx + 1) * CHUNK_SIZE]
        packet = b"IMG" + bytes([idx, total_chunks]) + chunk

        acked = False
        for attempt in range(1, MAX_RETRIES + 1):
            lora.send(packet)
            print("  chunk {}/{} sent (attempt {})".format(idx + 1, total_chunks, attempt))

            # Wait for ACK
            start = utime.ticks_ms()
            while utime.ticks_diff(utime.ticks_ms(), start) < ACK_TIMEOUT:
                reply = lora.poll_receive()
                if reply is not None:
                    raw = reply if isinstance(reply, bytes) else reply.encode()
                    if len(raw) >= 4 and raw[:3] == b"ACK" and raw[3] == idx:
                        print("  ACK received for chunk {}".format(idx))
                        acked = True
                        break
                utime.sleep_ms(10)

            if acked:
                break
            print("  timeout, retrying chunk {}...".format(idx))

        if not acked:
            print("ERROR: chunk {} failed after {} attempts, aborting.".format(idx, MAX_RETRIES))
            return False

        utime.sleep_ms(50)  # brief gap between chunks

    print("Transfer complete: {} bytes in {} chunks.".format(total, total_chunks))
    return True


if __name__ == "__main__":
    send_image("image.bin")

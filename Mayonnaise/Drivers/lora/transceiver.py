from machine import Pin, SPI
from Drivers.lora.lora import ULoRa


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

    def send(self, message):
        """
        Transmit a message string or bytes.

        :param message: str or bytes to send.
        """
        if isinstance(message, str):
            message = message.encode()
        self.lora.println(message)
        print("Sent: {}".format(message))

    def receive(self, timeout=5000):
        """
        Listen for an incoming packet.

        :param timeout: Timeout in milliseconds (default 5000 ms).
        :return: Decoded string payload, or None if nothing received.
        """
        payload = self.lora.listen(timeout=timeout)
        if payload:
            rssi = self.lora.packet_rssi()
            snr = self.lora.packet_snr()
            print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(payload, rssi, snr))
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

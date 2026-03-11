import utime
from Drivers.oled.oled_class import OLED
from Drivers.lora.transceiver import LoRaTransceiver


def main():
    oled = OLED()
    oled.display_text("Initialising\nLoRa...")

    try:
        radio = LoRaTransceiver()
        oled.display_text("LoRa OK\nReady")
        print("LoRa initialised OK")
    except Exception as e:
        oled.display_text("LoRa FAIL\n{}".format(e))
        print("LoRa init failed:", e)
        return

    counter = 0

    while True:
        msg = "Ping {}".format(counter)

        # --- TX ---
        oled.display_text("TX\n{}".format(msg))
        print("Sending:", msg)
        radio.send(msg)

        # --- RX ---
        oled.display_text("Listening...")
        print("Listening for reply...")
        reply = radio.receive(timeout=3000)

        if reply:
            rssi = radio.lora.packet_rssi()
            snr  = radio.lora.packet_snr()
            status_line = "RX OK\n{}\nRSSI:{}dBm\nSNR:{}dB".format(
                reply, rssi, snr
            )
            oled.display_text(status_line)
            print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(reply, rssi, snr))
        else:
            oled.display_text("No reply\nTX cnt:{}".format(counter))
            print("No reply received.")

        counter += 1
        utime.sleep_ms(2000)


main()

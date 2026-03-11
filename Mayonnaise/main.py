import utime
from Drivers.oled.oled_class import OLED
from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.uwb.bu03 import BU03


def main():
    oled = OLED()
    oled.display_text("Initialising...")

    # --- Init UWB ---
    uwb = BU03()

    # --- Init LoRa ---
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
        # --- UWB ---
        distances = uwb.read_distance()
        if distances:
            uwb.print_distances(distances)
            bs0 = distances[0]
            uwb_line = "UWB BS0:{:.2f}m".format(bs0) if bs0 else "UWB: No signal"
        else:
            uwb_line = "UWB: No data"

        # --- TX ---
        msg = "Ping {}".format(counter)
        print("Sending:", msg)
        radio.send(msg)

        # --- RX ---
        reply = radio.receive(timeout=3000)

        if reply:
            rssi = radio.lora.packet_rssi()
            snr  = radio.lora.packet_snr()
            lora_line = "RX:{}\nRSSI:{}dBm\nSNR:{}dB".format(reply, rssi, snr)
            print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(reply, rssi, snr))
        else:
            lora_line = "LoRa: No reply\nTX#{}".format(counter)
            print("No reply received.")

        oled.display_text("{}\n{}".format(uwb_line, lora_line))

        counter += 1
        utime.sleep_ms(2000)


main()

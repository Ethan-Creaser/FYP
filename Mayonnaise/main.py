import utime
from Drivers.oled.oled_class import OLED
from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.uwb.bu03 import BU03


def main():
    # --- Init OLED ---
    oled = None
    try:
        oled = OLED()
        oled.display_text("Initialising...")
        print("OLED initialised OK")
    except Exception as e:
        print("OLED init failed:", e)

    # --- Init UWB ---
    uwb = None
    try:
        uwb = BU03()
        print("UWB initialised OK")
    except Exception as e:
        print("UWB init failed:", e)

    # --- Init LoRa ---
    radio = None
    try:
        radio = LoRaTransceiver()
        print("LoRa initialised OK")
        if oled:
            oled.display_text("LoRa OK\nReady")
    except Exception as e:
        print("LoRa init failed:", e)
        if oled:
            oled.display_text("LoRa FAIL\n{}".format(e))

    counter = 0

    while True:
        # --- UWB ---
        uwb_line = "UWB: Not connected"
        if uwb is not None:
            try:
                distances = uwb.read_distance()
                if distances:
                    uwb.print_distances(distances)
                    bs0 = distances[0]
                    uwb_line = "UWB BS0:{:.2f}m".format(bs0) if bs0 else "UWB: No signal"
                else:
                    uwb_line = "UWB: No data"
            except Exception as e:
                uwb_line = "UWB ERR"
                print("UWB read error:", e)

        # --- LoRa TX/RX ---
        lora_line = "LoRa: Not connected"
        if radio is not None:
            try:
                msg = "Ping {}".format(counter)
                print("Sending:", msg)
                radio.send(msg)

                reply = radio.receive(timeout=3000)
                if reply:
                    rssi = radio.lora.packet_rssi()
                    snr  = radio.lora.packet_snr()
                    lora_line = "RX:{}\nRSSI:{}dBm\nSNR:{}dB".format(reply, rssi, snr)
                    print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(reply, rssi, snr))
                else:
                    lora_line = "LoRa: No reply\nTX#{}".format(counter)
                    print("No reply received.")
            except Exception as e:
                lora_line = "LoRa ERR"
                print("LoRa error:", e)

        # --- OLED ---
        if oled:
            try:
                oled.display_text("{}\n{}".format(uwb_line, lora_line))
            except Exception as e:
                print("OLED display error:", e)

        counter += 1
        utime.sleep_ms(2000)


main()

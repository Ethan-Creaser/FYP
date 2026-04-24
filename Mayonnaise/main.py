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
    last_sent = ""
    last_received = ""
    last_rssi = "-"
    last_snr = "-"

    try:
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
            status_line = "LoRa: Not connected"
            if radio is not None:
                try:
                    msg = "Ping {}".format(counter)
                    print("Sending:", msg)
                    radio.send(msg)
                    last_sent = msg

                    reply = radio.receive(timeout=1000)
                    if reply:
                        last_rssi = radio.lora.packet_rssi()
                        last_snr  = radio.lora.packet_snr()
                        last_received = reply
                        status_line = "RX OK"
                        print("Received: {} | RSSI: {} dBm | SNR: {} dB".format(reply, last_rssi, last_snr))
                    else:
                        status_line = "No reply TX#{}".format(counter)
                        print("No reply received.")
                except Exception as e:
                    status_line = "LoRa ERR"
                    print("LoRa error:", e)

            # --- OLED ---
            if oled:
                try:
                    screen = "{}\n{}\nRSSI:{}dBm\nSNR:{}dB\nTX:{}\nRX:{}".format(
                        uwb_line, status_line, last_rssi, last_snr, last_sent, last_received
                    )
                    oled.display_text(screen)
                except Exception as e:
                    print("OLED display error:", e)

            counter += 1
            utime.sleep_ms(500)

    except KeyboardInterrupt:
        print("Stopped.")

    except Exception as e:
        print("Unexpected error:", e)


main()

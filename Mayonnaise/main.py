import time
time.sleep(2)


import utime

from machine import Pin
from time import sleep
from neopixel import NeoPixel
pin = Pin(38, Pin.OUT)                          # Pin number for v1 of the above DevKitC, use pin 38 for v1.1
np = NeoPixel(pin, 1)  

try:
    import ujson as json
except ImportError:
    import json

try:
    from version import BUILD_ID, BUILD_NAME, FIRMWARE_VERSION
except ImportError:
    FIRMWARE_VERSION = "0.0.0"
    BUILD_ID = "unknown"
    BUILD_NAME = "unknown"

from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.oled.oled_class import OLED
from Drivers.thermistor import Thermistor
from Drivers.uwb.bu03 import BU03
from Drivers.wifi.wifi_logger import WiFiLogger
from Drivers.bt.bt_logger import BtLogger
try:
    import node as node_module
    EggNode = node_module.EggNode
except Exception as exc:
    print("NODE IMPORT FAILED:", exc)
    raise


LOG_WIDTH = 48


def print_section(title):
    print("")
    print("=" * LOG_WIDTH)
    print(title)
    print("=" * LOG_WIDTH)


def print_item(label, value):
    print("  {:<18} {}".format(label + ":", value))


DEFAULT_CONFIG = {
    "node_id": 0,
    "node_name": "egg_0",
    "device_label": "egg-00",
    "node_role": "field_egg",
    "ground_station_id": 0,
    "heartbeat_interval_ms": 30000,
    "sensor_interval_ms": 60000,
    "range_interval_ms": 10000,
    "display_interval_ms": 1000,
    "neighbour_suspect_ms": 75000,
    "neighbour_lost_ms": 120000,
    "repair_window_ms": 15000,
    "seen_cache_ms": 300000,
    "default_ttl": 5,
    "telemetry_enabled": True,
    "localisation_enabled": True,
    "localisation_boot_ms": 8000,
    "localisation_announce_ms": 1500,
    "localisation_turn_ms": 15000,
    "localisation_frames": 10,
    "localisation_max_members": 8,
    "rover_localise_interval_ms": 10000,
    "rover_localise_frames": 8,
    "rover_min_anchors": 2,
    "lora_frequency": 433000000,
    "lora_tx_power": 10,
    "lora_bandwidth": 125000,
    "lora_spreading_factor": 9,
    "uwb_id": 0,
    "uwb_role": 0,
    "uwb_channel": 1,
    "uwb_rate": 1,
    "thermistor_pin": None,
    "wifi_enabled": False,
    "wifi_mode": "sta",      # "sta" = join existing network, "ap" = create hotspot
    "wifi_ssid": None,
    "wifi_password": "",
    "wifi_port": 80,
    "bt_enabled": False,
}


def load_config(path="config.json"):
    config = DEFAULT_CONFIG.copy()
    try:
        with open(path, "r") as handle:
            user_config = json.load(handle)
        config.update(user_config)
        print_item("Config", "loaded from {}".format(path))
    except Exception as exc:
        print_item("Config", "using defaults ({})".format(exc))
    return config


def make_oled():
    try:
        oled = OLED()
        oled.display_text("Initialising...")
        print_item("OLED", "initialised")
        return oled
    except Exception as exc:
        print_item("OLED", "failed ({})".format(exc))
        return None


def make_radio(config, oled=None):
    parameters = {
        "frequency": config["lora_frequency"],
        "tx_power_level": config["lora_tx_power"],
        "signal_bandwidth": config["lora_bandwidth"],
        "spreading_factor": config["lora_spreading_factor"],
    }

    try:
        radio = LoRaTransceiver(parameters=parameters)
        print_item("LoRa", "initialised")
        if oled:
            oled.display_text("LoRa OK\nStarting node")
        return radio
    except Exception as exc:
        print_item("LoRa", "failed ({})".format(exc))
        if oled:
            oled.display_text("LoRa FAIL\n{}".format(exc))
        return None


def make_uwb(config):
    try:
        uwb = BU03()
        print_item("UWB", "initialised")
        return uwb
    except Exception as exc:
        print_item("UWB", "failed ({})".format(exc))
        return None


def make_wifi(config, oled=None):
    if not config.get("wifi_enabled", False):
        print_item("WiFi", "disabled")
        return None
    mode     = config.get("wifi_mode", "sta")
    ssid     = config.get("wifi_ssid") or config.get("node_name", "egg")
    password = config.get("wifi_password", "")
    port     = config.get("wifi_port", 80)
    try:
        wl = WiFiLogger(ssid=ssid, password=password, port=port, mode=mode)
        print_item("WiFi", "{}  ssid={}  ip={}  port={}".format(
            mode.upper(), ssid, wl.ip, port))
        if oled is not None:
            try:
                oled.display_text("WiFi {}\n{}\nport {}".format(
                    mode.upper(), wl.ip, port))
                import utime
                utime.sleep_ms(3000)
            except Exception:
                pass
        return wl
    except Exception as exc:
        print_item("WiFi", "failed ({})".format(exc))
        return None


def make_bt(config):
    if not config.get("bt_enabled", False):
        print_item("Bluetooth", "disabled")
        return None
    name = config.get("bt_name") or config.get("node_name", "egg")
    for attempt in range(1, 4):
        try:
            bt = BtLogger(name=name)
            print_item("Bluetooth", "advertising as '{}'".format(name))
            return bt
        except Exception as exc:
            print_item("Bluetooth", "attempt {} failed ({})".format(attempt, exc))
            if attempt < 3:
                import utime as _utime
                _utime.sleep_ms(500)
    return None


def make_thermistor(config):
    pin = config.get("thermistor_pin")
    if pin is None:
        print_item("Thermistor", "disabled")
        return None

    try:
        thermistor = Thermistor(pin)
        print_item("Thermistor", "initialised on pin {}".format(pin))
        return thermistor
    except Exception as exc:
        print_item("Thermistor", "failed ({})".format(exc))
        return None


def main():
    np[0] = (255,0,0) # red
    np.write()
    sleep(0.1)
    print_section("EGG NODE BOOT")
    print_item("Serial", "connected")
    print_item("Firmware", FIRMWARE_VERSION)
    print_item("Build ID", BUILD_ID)
    print_item("Build Name", BUILD_NAME)

    config = load_config()

    print_section("HARDWARE STARTUP")
    oled = make_oled()
    radio = make_radio(config, oled)
    uwb = make_uwb(config)
    thermistor = make_thermistor(config)
    wifi = make_wifi(config, oled)
    bt   = make_bt(config)

    node = EggNode(config, radio, uwb=uwb, thermistor=thermistor, oled=oled)
    if wifi is not None:
        node.logger.add_output(wifi)
    if bt is not None:
        node.logger.add_output(bt)

    print_section("NODE STARTED")
    print_item("Device", config["device_label"])
    print_item("Node", "{} ({})".format(config["node_name"], config["node_id"]))
    print_item("Role", config["node_role"])
    print_item("Ground station", config["ground_station_id"])
    print_item("UWB ID", config["uwb_id"])
    print_item("Heartbeat", "{} ms".format(config["heartbeat_interval_ms"]))
    print_item("Telemetry", "enabled" if config["telemetry_enabled"] else "disabled")
    print_item("Localisation", "enabled" if config["localisation_enabled"] else "disabled")
    print_item("LoRa frequency", "{} Hz".format(config["lora_frequency"]))

    try:
        while True:
            node.poll(utime.ticks_ms())
            if wifi is not None:
                wifi.poll()
            if bt is not None:
                bt.poll()
            alive, _, _ = node.neighbours.summary()
            if alive >= 2:
                np[0] = (0,255,0) # green
                np.write()
            elif alive >= 1:
                np[0] = (255,255,0) # yellow
                np.write()
            else:
                np[0] = (255,0,0) # red
                np.write()
            utime.sleep_ms(50)
    except KeyboardInterrupt:
        print_section("NODE STOPPED")


main()

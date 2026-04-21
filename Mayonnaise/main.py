import time
time.sleep(2)

import utime

try:
    import ujson as json
except ImportError:
    import json

from Drivers.lora.transceiver import LoRaTransceiver
from Drivers.oled.oled_class import OLED
from Drivers.thermistor import Thermistor
from Drivers.uwb.bu03 import BU03
from node import EggNode


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
    "base_station_id": 0,
    "heartbeat_interval_ms": 30000,
    "sensor_interval_ms": 60000,
    "range_interval_ms": 10000,
    "display_interval_ms": 1000,
    "neighbour_suspect_ms": 75000,
    "neighbour_lost_ms": 120000,
    "repair_window_ms": 15000,
    "seen_cache_ms": 300000,
    "default_ttl": 5,
    "lora_frequency": 433000000,
    "lora_tx_power": 10,
    "lora_bandwidth": 125000,
    "lora_spreading_factor": 9,
    "uwb_id": 0,
    "uwb_role": 0,
    "uwb_channel": 1,
    "uwb_rate": 1,
    "thermistor_pin": None,
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
    print_section("EGG NODE BOOT")
    print_item("Serial", "connected")

    config = load_config()

    print_section("HARDWARE STARTUP")
    oled = make_oled()
    radio = make_radio(config, oled)
    uwb = make_uwb(config)
    thermistor = make_thermistor(config)

    node = EggNode(config, radio, uwb=uwb, thermistor=thermistor, oled=oled)

    print_section("NODE STARTED")
    print_item("Node", "{} ({})".format(config["node_name"], config["node_id"]))
    print_item("Base station", config["base_station_id"])
    print_item("Heartbeat", "{} ms".format(config["heartbeat_interval_ms"]))
    print_item("LoRa frequency", "{} Hz".format(config["lora_frequency"]))

    try:
        while True:
            node.poll(utime.ticks_ms())
            utime.sleep_ms(50)
    except KeyboardInterrupt:
        print_section("NODE STOPPED")


main()

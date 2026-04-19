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
    except Exception as exc:
        print("Config load failed, using defaults:", exc)
    return config


def make_oled():
    try:
        oled = OLED()
        oled.display_text("Initialising...")
        print("OLED initialised OK")
        return oled
    except Exception as exc:
        print("OLED init failed:", exc)
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
        print("LoRa initialised OK")
        if oled:
            oled.display_text("LoRa OK\nStarting node")
        return radio
    except Exception as exc:
        print("LoRa init failed:", exc)
        if oled:
            oled.display_text("LoRa FAIL\n{}".format(exc))
        return None


def make_uwb(config):
    try:
        uwb = BU03()
        print("UWB initialised OK")
        return uwb
    except Exception as exc:
        print("UWB init failed:", exc)
        return None


def make_thermistor(config):
    pin = config.get("thermistor_pin")
    if pin is None:
        print("Thermistor disabled: set thermistor_pin in config.json")
        return None

    try:
        thermistor = Thermistor(pin)
        print("Thermistor initialised OK on pin {}".format(pin))
        return thermistor
    except Exception as exc:
        print("Thermistor init failed:", exc)
        return None


def main():
    config = load_config()
    oled = make_oled()
    radio = make_radio(config, oled)
    uwb = make_uwb(config)
    thermistor = make_thermistor(config)

    node = EggNode(config, radio, uwb=uwb, thermistor=thermistor, oled=oled)
    print("Egg node {} started".format(config["node_id"]))

    try:
        while True:
            node.poll(utime.ticks_ms())
            utime.sleep_ms(50)
    except KeyboardInterrupt:
        print("Stopped.")


main()

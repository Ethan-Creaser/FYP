"""Minimal main for ESP32 (MicroPython) to run the mesh node.

Behavior:
- load `config.json`
- create `Node`
- attach hardware radio if `use_hardware` is true
- run periodic beaconing and poll the radio when required

This file is intentionally small — extend per your application needs.
"""

try:
    import utime as time
except Exception:
    import time

import json
import random
import packets

from node import Node
import constants


def main():
    cfg_path = "config.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print("Failed to load config.json:", e)
        return

    # prefer identity.bin when available (written by hardcode_egg_id.py)
    try:
        from identity import get_ids
        node_id, uwb_id = get_ids(cfg_path=cfg_path)
    except Exception:
        node_id = int(cfg.get("node_id", 1))
        uwb_id = None

    allowed = cfg.get("allowed_neighbors")
    allowlist = set(allowed) if allowed else None

    node = Node(node_id, allowlist=allowlist)

    if cfg.get("use_hardware"):
        ok = node.attach_hardware_from_config(cfg_path)
        if not ok:
            print("Hardware attach failed — running without radio glue")
        else:
            print("Hardware radio attached")
            # try to start background poll if adapter supports it
            try:
                if getattr(node, "radio", None) and hasattr(node.radio, "start_background"):
                    node.radio.start_background(timeout_ms=500)
                    print("Radio background poll started")
            except Exception as e:
                print("Could not start radio background:", e)

        # attach OLED/status if available (log and force redraw)
        try:
            from oled_status import OLEDStatus
            display = OLEDStatus()
            try:
                display.attach_node(node)
                print("OLED attached")
                try:
                    # force an initial redraw so the display shows state on boot
                    display._redraw()
                except Exception:
                    pass
            except Exception as e:
                print("OLED attach failed:", e)
            node.display = display
        except Exception as e:
            print("OLED import failed:", e)
            display = None

        # attach UWB module if enabled
        if cfg.get("use_uwb"):
            try:
                from Drivers.uwb.bu03 import BU03
                p = cfg.get("uwb_pins", {})
                node.uwb = BU03(
                    data_uart_id   = p.get("data_uart_id",   1),
                    data_tx        = p.get("data_tx",        17),
                    data_rx        = p.get("data_rx",        18),
                    config_uart_id = p.get("config_uart_id", 2),
                    config_tx      = p.get("config_tx",      2),
                    config_rx      = p.get("config_rx",      1),
                    reset_pin      = p.get("reset_pin",      15),
                )
                initial_role = 0 if (uwb_id or 0) == 0 else 1
                node.uwb.configure_warm(uwb_id or 0, initial_role)
                print("UWB attached: id={} role={}".format(uwb_id, initial_role))
            except Exception as e:
                print("UWB init failed:", e)

        # attach BLE logger, tee all prints over it, and handle incoming commands
        if cfg.get("use_bluetooth"):
            try:
                import builtins
                from Drivers.bt.bt_logger import BtLogger
                bt_name = cfg.get("bt_name") or "egg_{}".format(node_id)
                _bt = BtLogger(name=bt_name)
                node.bt_logger = _bt

                # tee every print() call to BLE so the PC sees all serial output
                _orig_print = builtins.print
                def _tee_print(*args, **kwargs):
                    _orig_print(*args, **kwargs)
                    sep  = kwargs.get("sep", " ")
                    line = sep.join(str(a) for a in args)
                    try:
                        _bt.log(line)
                    except Exception:
                        pass
                builtins.print = _tee_print

                # handle UWB config command from the PC
                # payload: [0xCF, target_egg_id, uwb_id, role]
                _BT_CMD_UWB = 0xCF
                def _bt_rx(data):
                    if len(data) < 4 or data[0] != _BT_CMD_UWB:
                        print("BT: unknown command:", list(data))
                        return
                    target_id = data[1]
                    uwb_id_cmd = data[2]
                    role       = data[3]
                    print("BT CMD: target={} uwb_id={} role={}".format(
                        target_id, uwb_id_cmd, role))
                    node.send_data(
                        target_id,
                        constants.APP_CTRL,
                        constants.CTRL_UWB_CONFIG,
                        bytes([uwb_id_cmd, role]),
                    )
                    print("UWB config sent to egg_{}".format(target_id))

                _bt.on_rx = _bt_rx
                print("BT logger started as", bt_name)
            except Exception as e:
                print("BT logger init failed:", e)

        # production: no periodic hardware test in main.py (use Debug/hw_runner.py)

    beacon_interval = getattr(constants, "BEACON_INTERVAL", 30)
    beacon_jitter   = getattr(constants, "BEACON_JITTER", 5)
    next_beacon = time.time()

    try:
        while True:
            now = time.time()
            if now >= next_beacon:
                # Beacon suppression: if we transmitted anything within the last
                # beacon_interval seconds, neighbours already know we are alive.
                # Skip this beacon and let the timer fire again next cycle.
                if now - node._last_tx_time >= beacon_interval:
                    node.send_beacon()
                jitter = (random.random() - 0.5) * 2 * beacon_jitter
                next_beacon = now + beacon_interval + jitter

            # If radio exists and no background thread, poll it here (blocking short)
            if getattr(node, "radio", None) and not getattr(node.radio, "_bg_running", False):
                try:
                    node.radio.poll(timeout_ms=200)
                except Exception as e:
                    print("radio poll error:", e)

            # retry / expire stale RREQs
            node.tick()

            # process any BLE RX buffered from the IRQ
            if getattr(node, "bt_logger", None):
                node.bt_logger.poll()

            # production main: periodic application behavior only (no built-in hw test here)

            # small sleep to yield
            try:
                time.sleep(0.1)
            except Exception:
                # fallback for utime which may not support float sleep
                try:
                    time.sleep_ms(100)
                except Exception:
                    pass

    except KeyboardInterrupt:
        print("main: interrupted, exiting")
        if getattr(node, "radio", None) and getattr(node.radio, "_bg_running", False):
            node.radio.stop_background()


if __name__ == "__main__":
    main()

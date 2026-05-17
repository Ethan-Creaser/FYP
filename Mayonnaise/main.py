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

from node import Node
import constants
try:
    from version import VERSION
except Exception:
    VERSION = "unknown"


def _attach_uwb(loc, cfg, uwb_id, attempts=3, retry_delay_ms=2000):
    """Initialise the BU03 UWB module and attach it to the localise_app.

    Retries up to `attempts` times.  Each attempt:
      1. Constructs the BU03 (sets up UARTs, 500 ms settle)
      2. Calls configure_warm() (~5.5 s: three AT commands + warm reset)
    On success sets loc.uwb and loc.uwb_default_id.
    """
    try:
        from Drivers.uwb.bu03 import BU03
    except Exception as e:
        print("UWB driver import failed:", e)
        return

    p            = cfg.get("uwb_pins", {})
    initial_role = 0 if (uwb_id or 0) == 0 else 1

    for attempt in range(1, attempts + 1):
        print("UWB init attempt {}/{}...".format(attempt, attempts))
        try:
            _uwb = BU03(
                data_uart_id   = p.get("data_uart_id",   1),
                data_tx        = p.get("data_tx",        17),
                data_rx        = p.get("data_rx",        18),
                config_uart_id = p.get("config_uart_id", 2),
                config_tx      = p.get("config_tx",      2),
                config_rx      = p.get("config_rx",      1),
                reset_pin      = p.get("reset_pin",      15),
            )
        except Exception as e:
            print("UWB UART init failed (attempt {}): {}".format(attempt, e))
            if attempt < attempts:
                try:
                    import utime
                    utime.sleep_ms(retry_delay_ms)
                except Exception:
                    pass
            continue

        try:
            _uwb.configure(uwb_id or 0, initial_role)
        except Exception as e:
            print("UWB configure failed (attempt {}): {}".format(attempt, e))
            if attempt < attempts:
                try:
                    import utime
                    utime.sleep_ms(retry_delay_ms)
                except Exception:
                    pass
            continue

        # Both steps succeeded
        loc.uwb = _uwb
        loc.uwb_default_id = uwb_id or 0
        print("UWB attached: id={} role={} (attempt {})".format(
            uwb_id, initial_role, attempt))
        return

    print("UWB init FAILED after {} attempts — UWB not available".format(attempts))


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

    try:
        from identity import get_allowed_neighbors
        allowlist = get_allowed_neighbors()
    except Exception:
        allowlist = None

    node = Node(node_id, allowlist=allowlist)

    # Ping state machine — set by _bt_rx, consumed by the main loop.
    # Fires n_left DATA packets at target, one every _PING_INTERVAL_MS milliseconds.
    try:
        from utime import ticks_ms as _pticks, ticks_diff as _pdiff
    except ImportError:
        import time as _pt
        def _pticks(): return int(_pt.time() * 1000)
        def _pdiff(a, b): return a - b
    _PING_INTERVAL_MS = 400   # ms between packets (safe LoRa SF9 airtime)
    _ping_state = {"active": False, "target": 0, "n_left": 0, "n_total": 0, "last_tx": 0}

    try:
        from identity import get_beacon_enabled
        node.beacon_enabled = get_beacon_enabled()
    except Exception:
        node.beacon_enabled = True

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

        # attach NeoPixel LED if available
        try:
            from led_status import LEDStatus
            _neo_pin = cfg.get("neopixel_pin", 38)
            _led = LEDStatus(pin=_neo_pin)
            node.led = _led
            print("NeoPixel LED attached on pin", _neo_pin)
        except Exception as e:
            print("NeoPixel LED init failed:", e)

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

                # BLE command bytes from the PC
                # 0xCF [target_id, uwb_id, role]              → UWB config (reconfigure + scan)
                # 0xD0 [target_id]                            → UWB restore to identity.bin default
                # 0xD1 [target_id, uwb_id, count, n0, n1...]  → rewrite identity.bin + live allowlist
                # 0xD2 [target_id, 0/1]                       → disable/enable beaconing, persists
                # 0xD3 [target_id, n_packets]                 → fire n_packets pings at target (RSSI/RTT test)
                _BT_CMD_UWB          = 0xCF
                _BT_CMD_UWB_RESTORE  = 0xD0
                _BT_CMD_IDENTITY     = 0xD1
                _BT_CMD_BEACON       = 0xD2
                _BT_CMD_PING         = 0xD3
                def _bt_rx(data):
                    if not data:
                        return
                    cmd = data[0]
                    if cmd == _BT_CMD_UWB:
                        if len(data) < 4:
                            print("BT: UWB config too short:", list(data))
                            return
                        target_id  = data[1]
                        uwb_id_cmd = data[2]
                        role       = data[3]
                        print("BT CMD UWB_CONFIG: target={} uwb_id={} role={}".format(
                            target_id, uwb_id_cmd, role))
                        node.send_data(
                            target_id,
                            constants.APP_CTRL,
                            constants.CTRL_UWB_CONFIG,
                            bytes([uwb_id_cmd, role]),
                        )
                        print("UWB config sent to egg_{}".format(target_id))
                    elif cmd == _BT_CMD_UWB_RESTORE:
                        if len(data) < 2:
                            print("BT: UWB restore too short:", list(data))
                            return
                        target_id = data[1]
                        print("BT CMD UWB_RESTORE: target={}".format(target_id))
                        node.send_data(
                            target_id,
                            constants.APP_CTRL,
                            constants.CTRL_UWB_RESTORE,
                            b"",
                        )
                        print("UWB restore sent to egg_{}".format(target_id))
                    elif cmd == _BT_CMD_IDENTITY:
                        # [0xD1, target_id, uwb_id, neighbor_count, n0, n1, ...]
                        if len(data) < 4:
                            print("BT: identity write too short:", list(data))
                            return
                        target_id  = data[1]
                        uwb_id_cmd = data[2]
                        count      = data[3]
                        if len(data) < 4 + count:
                            print("BT: identity write truncated (got {} need {})".format(
                                len(data), 4 + count))
                            return
                        neighbors = list(data[4:4 + count])
                        print("BT CMD IDENTITY_WRITE: target={} uwb_id={} neighbors={}".format(
                            target_id, uwb_id_cmd, neighbors))
                        if target_id == node.node_id:
                            try:
                                from identity import write_identity, read_identity
                                existing = read_identity()
                                cur_beacon = existing[3] if existing else True
                                write_identity(node.node_id, uwb_id_cmd,
                                               allowed_neighbors=neighbors or None,
                                               beacon_enabled=cur_beacon)
                                node.neighbours.allowlist = set(neighbors) if neighbors else None
                                # Machine-parseable confirmation — PC script watches for this
                                nb_str = ",".join(str(n) for n in neighbors)
                                print("IDENTITY_OK node_id={} uwb_id={} neighbors={}".format(
                                    node.node_id, uwb_id_cmd, nb_str))
                            except Exception as e:
                                print("IDENTITY_FAIL reason={}".format(e))
                        else:
                            mesh_payload = bytearray([uwb_id_cmd, count] + neighbors)
                            node.send_data(target_id, constants.APP_CTRL,
                                           constants.CTRL_IDENTITY_WRITE, bytes(mesh_payload))
                            print("Identity write relayed to egg_{}".format(target_id))
                            # IDENTITY_OK will arrive later as CTRL_IDENTITY_ACK via the mesh
                    elif cmd == _BT_CMD_PING:
                        if len(data) < 3:
                            print("BT: ping too short:", list(data))
                            return
                        target_id = data[1]
                        n_packets = max(1, int(data[2]))
                        print("BT CMD PING: target={} n={}".format(target_id, n_packets))
                        _ping_state["active"] = True
                        _ping_state["target"] = target_id
                        _ping_state["n_left"] = n_packets
                        _ping_state["n_total"] = n_packets
                        _ping_state["last_tx"] = 0
                        print("PING_START node={} dst={} n={}".format(
                            node.node_id, target_id, n_packets))
                    elif cmd == _BT_CMD_BEACON:
                        if len(data) < 3:
                            print("BT: beacon cmd too short:", list(data))
                            return
                        target_id = data[1]
                        enabled   = bool(data[2])
                        print("BT CMD BEACON_{}: target={}".format(
                            "ENABLE" if enabled else "DISABLE", target_id))
                        if target_id == node.node_id:
                            try:
                                from identity import write_identity, read_identity
                                existing = read_identity()
                                if existing:
                                    write_identity(existing[0], existing[1],
                                                   allowed_neighbors=existing[2],
                                                   beacon_enabled=enabled)
                                node.beacon_enabled = enabled
                                print("BEACON_OK node_id={} enabled={}".format(
                                    node.node_id, int(enabled)))
                            except Exception as e:
                                print("BEACON_FAIL reason={}".format(e))
                        else:
                            node.send_data(target_id, constants.APP_CTRL,
                                           constants.CTRL_BEACON, bytes([1 if enabled else 0]))
                            print("Beacon cmd relayed to egg_{}".format(target_id))
                    else:
                        print("BT: unknown command:", list(data))

                _bt.on_rx = _bt_rx
                print("BT logger started as", bt_name)
            except Exception as e:
                print("BT logger init failed:", e)

        # attach localisation app if enabled, then wire UWB into it
        if cfg.get("localisation_enabled"):
            try:
                from app_localise import LocaliseApp
                LocaliseApp(node)
                print("Localisation app attached")
            except Exception as e:
                print("Localisation app init failed:", e)

        # attach UWB module into localise_app (requires localise_app to exist)
        if cfg.get("use_uwb"):
            loc = getattr(node, "localise_app", None)
            if loc is None:
                print("UWB init skipped: localisation_enabled must be true to use UWB")
            else:
                _attach_uwb(loc, cfg, uwb_id)

        # production: no periodic hardware test in main.py (use Debug/hw_runner.py)

    _loc    = getattr(node, "localise_app", None)
    _radio  = getattr(node, "radio", None)
    _hw = [
        ("radio", _radio is not None,                              cfg.get("use_hardware")),
        ("bt",    getattr(node, "bt_logger", None) is not None,   cfg.get("use_bluetooth")),
        ("oled",  getattr(node, "display",   None) is not None,   cfg.get("use_hardware")),
        ("uwb",   _loc is not None and getattr(_loc, "uwb", None) is not None, cfg.get("use_uwb")),
    ]
    
    print("=" * 32)
    print("  Mayonnaise v{}  node {}".format(VERSION, node_id))
    print("  " + "-" * 28)
    for name, ok, wanted in _hw:
        if not wanted:
            status = "off"
        elif ok:
            status = "OK"
        else:
            status = "FAIL"
        print("  {:<8} {}".format(name, status))
    print("  {:<8} {}".format("beacon", "ON" if node.beacon_enabled else "OFF"))
    print("=" * 32)

    _oled = getattr(node, "display", None)
    if _oled is not None:
        lines = ["v{}  node {}".format(VERSION, node_id)]
        for name, ok, wanted in _hw:
            if name == "oled":
                continue
            if not wanted:
                status = "off"
            elif ok:
                status = "OK"
            else:
                status = "FAIL"
            lines.append("{}: {}".format(name, status))
        _oled.display_text("\n".join(lines))

    _led = getattr(node, "led", None)
    if _led:
        try:
            _led.set_idle()
        except Exception:
            pass

    beacon_interval = getattr(constants, "BEACON_INTERVAL", 30)
    beacon_jitter   = getattr(constants, "BEACON_JITTER", 5)
    next_beacon = time.time()
    next_tick   = time.time() + 5

    try:
        while True:
            now = time.time()

            if now >= next_tick:
                node.tick()
                next_tick = now + 5

            if now >= next_beacon:
                # Beacon suppression: if we transmitted anything within the last
                # beacon_interval seconds, neighbours already know we are alive.
                # Skip this beacon and let the timer fire again next cycle.
                if node.beacon_enabled and now - node._last_tx_time >= beacon_interval:
                    node.send_beacon()
                jitter = (random.random() - 0.5) * 2 * beacon_jitter
                next_beacon = now + beacon_interval + jitter

            if _ping_state["active"] and _pdiff(_pticks(), _ping_state["last_tx"]) >= _PING_INTERVAL_MS:
                if _ping_state["n_left"] > 0:
                    node.send_data(_ping_state["target"], constants.APP_CTRL,
                                   constants.CTRL_PING, b"ping")
                    _ping_state["n_left"] -= 1
                    _ping_state["last_tx"] = _pticks()
                else:
                    _ping_state["active"] = False
                    print("PING_DONE node={} dst={} n_sent={}".format(
                        node.node_id, _ping_state["target"], _ping_state["n_total"]))

            # If radio exists and no background thread, poll it here (blocking short)
            if getattr(node, "radio", None) and not getattr(node.radio, "_bg_running", False):
                try:
                    node.radio.poll(timeout_ms=200)
                except Exception as e:
                    print("radio poll error:", e)

            # process any BLE RX buffered from the IRQ
            if getattr(node, "bt_logger", None):
                node.bt_logger.poll()

            # advance NeoPixel LED state machine
            if getattr(node, "led", None):
                try:
                    node.led.poll()
                except Exception:
                    pass

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

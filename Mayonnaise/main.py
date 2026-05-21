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

    try:
        from boot_id import load_boot_id
        _boot_id = load_boot_id()
    except Exception as e:
        _boot_id = random.randint(1, 254)
        print("boot_id fallback (no boot_id.bin):", _boot_id, e)

    node = Node(node_id, boot_id=_boot_id, allowlist=allowlist)

    if node_id == constants.GROUND_STATION_ID and allowlist:
        for _nid in allowlist:
            node.routes.set_route(_nid, _nid, 1)
        print("GS: pre-populated routes for", sorted(allowlist))
    else:
        # GS is always 1 hop — pre-populate so eggs never flood RREQ for it
        node.routes.set_route(constants.GROUND_STATION_ID, constants.GROUND_STATION_ID, 1)
        print("pre-populated route to GS ({})".format(constants.GROUND_STATION_ID))

    from range_test import PingState
    _ping = PingState()

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
                # 0xD4 [target_id]                            → query alive neighbours (0xFF = broadcast all)
                # 0xD5 [target_id]                            → query route table (0xFF = broadcast all)
                # 0xD6 [target_id, 0/1]                       → disable/enable UWB (hold/release reset pin)
                _BT_CMD_UWB             = 0xCF
                _BT_CMD_UWB_RESTORE     = 0xD0
                _BT_CMD_IDENTITY        = 0xD1
                _BT_CMD_BEACON          = 0xD2
                _BT_CMD_PING            = 0xD3
                _BT_CMD_GET_NEIGHBOURS  = 0xD4
                _BT_CMD_GET_ROUTES      = 0xD5
                _BT_CMD_UWB_DISABLE     = 0xD6
                _BT_CMD_RESET_STATE     = 0xD7
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
                                cur_uwb_en = existing[4] if existing else True
                                write_identity(node.node_id, uwb_id_cmd,
                                               allowed_neighbors=neighbors or None,
                                               beacon_enabled=cur_beacon,
                                               uwb_enabled=cur_uwb_en)
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
                        _ping.start(node, target_id, n_packets)
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
                                                   beacon_enabled=enabled,
                                                   uwb_enabled=existing[4])
                                node.beacon_enabled = enabled
                                print("BEACON_OK node_id={} enabled={}".format(
                                    node.node_id, int(enabled)))
                            except Exception as e:
                                print("BEACON_FAIL reason={}".format(e))
                        else:
                            node.send_data(target_id, constants.APP_CTRL,
                                           constants.CTRL_BEACON, bytes([1 if enabled else 0]))
                            print("Beacon cmd relayed to egg_{}".format(target_id))
                    elif cmd == _BT_CMD_GET_NEIGHBOURS:
                        if len(data) < 2:
                            print("BT: get_neighbours too short:", list(data))
                            return
                        target_id = data[1]
                        if target_id == 0xFF:
                            import packets as _packets
                            seq = node.next_seq()
                            pkt = _packets.make_bcast(
                                src=node.node_id, seq=seq, ttl=1,
                                app_id=constants.APP_CTRL,
                                subtype=constants.CTRL_GET_NEIGHBOURS,
                                data=bytes([target_id]),
                            )
                            node.send_packet(pkt)
                            print("GET_NEIGHBOURS broadcast seq={}".format(seq))
                            node._handle_mesh_ctrl(node.node_id, constants.CTRL_GET_NEIGHBOURS,
                                                   bytes([target_id]))
                        elif target_id == node.node_id:
                            node._handle_mesh_ctrl(node.node_id, constants.CTRL_GET_NEIGHBOURS,
                                                   bytes([target_id]))
                        else:
                            node.send_data(target_id, constants.APP_CTRL,
                                           constants.CTRL_GET_NEIGHBOURS, bytes([target_id]))
                            print("GET_NEIGHBOURS sent target={}".format(target_id))
                    elif cmd == _BT_CMD_GET_ROUTES:
                        if len(data) < 2:
                            print("BT: get_routes too short:", list(data))
                            return
                        target_id = data[1]
                        if target_id == 0xFF:
                            import packets as _packets
                            seq = node.next_seq()
                            pkt = _packets.make_bcast(
                                src=node.node_id, seq=seq, ttl=1,
                                app_id=constants.APP_CTRL,
                                subtype=constants.CTRL_GET_ROUTES,
                                data=bytes([target_id]),
                            )
                            node.send_packet(pkt)
                            print("GET_ROUTES broadcast seq={}".format(seq))
                            node._handle_mesh_ctrl(node.node_id, constants.CTRL_GET_ROUTES,
                                                   bytes([target_id]))
                        elif target_id == node.node_id:
                            node._handle_mesh_ctrl(node.node_id, constants.CTRL_GET_ROUTES,
                                                   bytes([target_id]))
                        else:
                            node.send_data(target_id, constants.APP_CTRL,
                                           constants.CTRL_GET_ROUTES, bytes([target_id]))
                            print("GET_ROUTES sent target={}".format(target_id))
                    elif cmd == _BT_CMD_UWB_DISABLE:
                        if len(data) < 3:
                            print("BT: uwb_disable too short:", list(data))
                            return
                        target_id = data[1]
                        enabled   = bool(data[2])
                        print("BT CMD UWB_{}: target={}".format(
                            "ENABLE" if enabled else "DISABLE", target_id))
                        if target_id == node.node_id:
                            loc = getattr(node, "localise_app", None)
                            if loc is not None and loc.uwb is not None:
                                try:
                                    from identity import write_identity, read_identity
                                    existing = read_identity()
                                    if existing:
                                        write_identity(existing[0], existing[1],
                                                       allowed_neighbors=existing[2],
                                                       beacon_enabled=existing[3],
                                                       uwb_enabled=enabled)
                                except Exception as e:
                                    print("UWB_DISABLE identity write failed:", e)
                                if enabled:
                                    loc._uwb_enable_pending = True
                                    print("UWB_ENABLE_PENDING node_id={}".format(node.node_id))
                                else:
                                    loc._uwb_enabled = False
                                    loc.uwb.power_off()
                                    print("UWB_OK node_id={} enabled=0".format(node.node_id))
                            else:
                                print("UWB_DISABLE: no UWB attached node_id={}".format(node.node_id))
                        else:
                            node.send_data(target_id, constants.APP_CTRL,
                                           constants.CTRL_UWB_DISABLE,
                                           bytes([1 if enabled else 0]))
                            print("UWB {} relayed to egg_{}".format(
                                "ENABLE" if enabled else "DISABLE", target_id))
                    elif cmd == _BT_CMD_RESET_STATE:
                        print("BT CMD RESET_STATE: broadcasting mesh state reset")
                        node.send_ctrl_bcast(constants.CTRL_RESET_STATE)
                        print("RESET_STATE_OK")
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
            if uwb_id is None:
                print("UWB init skipped: uwb_id=None in identity.bin")
            else:
                loc = getattr(node, "localise_app", None)
                if loc is None:
                    print("UWB init skipped: localisation_enabled must be true to use UWB")
                else:
                    try:
                        from identity import get_uwb_enabled
                        _uwb_en = get_uwb_enabled()
                    except Exception:
                        _uwb_en = True
                    if not _uwb_en:
                        # Disabled in identity.bin — create BU03 (fast, no configure) then
                        # power off immediately so the 5 s AT+SETCFG sequence is skipped.
                        try:
                            from Drivers.uwb.bu03 import BU03
                            p = cfg.get("uwb_pins", {})
                            _uwb = BU03(
                                data_uart_id   = p.get("data_uart_id",   1),
                                data_tx        = p.get("data_tx",        17),
                                data_rx        = p.get("data_rx",        18),
                                config_uart_id = p.get("config_uart_id", 2),
                                config_tx      = p.get("config_tx",      2),
                                config_rx      = p.get("config_rx",      1),
                                reset_pin      = p.get("reset_pin",      15),
                            )
                            _uwb.power_off()
                            loc.uwb = _uwb
                            loc.uwb_default_id = uwb_id or 0
                            loc._uwb_enabled = False
                            print("UWB init skipped (disabled)")
                        except Exception as e:
                            print("UWB disabled-boot setup failed:", e)
                    else:
                        _attach_uwb(loc, cfg, uwb_id)

        # production: no periodic hardware test in main.py (use Debug/hw_runner.py)

    _loc    = getattr(node, "localise_app", None)
    _radio  = getattr(node, "radio", None)
    _uwb_attached  = _loc is not None and getattr(_loc, "uwb", None) is not None
    _uwb_enabled   = getattr(_loc, "_uwb_enabled", True) if _loc else True
    _hw = [
        ("radio", _radio is not None,                            cfg.get("use_hardware")),
        ("bt",    getattr(node, "bt_logger", None) is not None, cfg.get("use_bluetooth")),
        ("oled",  getattr(node, "display",   None) is not None, cfg.get("use_hardware")),
        ("uwb",   _uwb_attached,                                 cfg.get("use_uwb")),
    ]

    print("=" * 32)
    print("  Mayonnaise v{}  node {}".format(VERSION, node_id))
    print("  " + "-" * 28)
    for name, ok, wanted in _hw:
        if name == "uwb" and wanted and ok and not _uwb_enabled:
            status = "DISABLED"
        elif not wanted:
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
            if name == "uwb" and wanted and ok and not _uwb_enabled:
                status = "DISABLED"
            elif not wanted:
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

    beacon_interval      = getattr(constants, "BEACON_INTERVAL", 30)
    beacon_interval_fast = getattr(constants, "BEACON_INTERVAL_FAST", 10)
    beacon_fast_duration = getattr(constants, "BEACON_FAST_DURATION", 60)
    beacon_jitter        = getattr(constants, "BEACON_JITTER", 5)
    next_beacon = time.time()
    next_tick   = time.time() + 5

    try:
        while True:
            now = time.time()

            if now >= next_tick:
                node.tick()
                next_tick = now + 5

            # After CTRL_RESET_STATE, reschedule beacon with per-node jitter to
            # spread out post-reset transmissions and avoid radio collisions.
            if getattr(node, "_beacon_reset", False):
                node._beacon_reset = False
                next_beacon = now   # beacon already sent in reset_state(); just reset scheduler

            if now >= next_beacon:
                # Use fast interval for beacon_fast_duration seconds after boot
                # or after a reset (node.start_time is updated by reset_state()).
                current_interval = (
                    beacon_interval_fast
                    if now - node.start_time < beacon_fast_duration
                    else beacon_interval
                )
                # Beacon suppression: if we transmitted anything within the last
                # interval seconds, neighbours already know we are alive.
                # Skip this beacon and let the timer fire again next cycle.
                if node.beacon_enabled and now - node._last_tx_time >= current_interval:
                    node.send_beacon()
                jitter = (random.random() - 0.5) * 2 * beacon_jitter
                next_beacon = now + current_interval + jitter

            _ping.poll(node)

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

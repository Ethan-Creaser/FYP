import gc
import utime
import urandom

try:
    import uos as os
except ImportError:
    import os

try:
    import ujson as json
except ImportError:
    import json

from localisation.controller import LocalisationController
from localisation.pc_ranging import PCRangingController
from neighbour_table import NeighbourTable
from node_logger import NodeLogger
from timers import due, elapsed_ms
from packets import (
    BROADCAST,
    HEARTBEAT,
    HELLO,
    IMAGE_OFFER,
    LOCALISE_DISCOVERY,
    LOCALISE_POSITION,
    LOCALISE_RESULT,
    LOCALISE_START,
    LOCALISE_TURN,
    RANGE_REPORT,
    ROVER_START,
    ROVER_STOP,
    SENSOR_REPORT,
    decode_packet,
    encode_packet,
    is_for_node,
    make_packet,
    packet_uid,
    relay_copy,
)

LOCALISATION_PACKET_TYPES = (
    LOCALISE_DISCOVERY,
    LOCALISE_START,
    LOCALISE_POSITION,
    LOCALISE_RESULT,
    LOCALISE_TURN,
)

ROLE_FIELD_EGG = "field_egg"
ROLE_ROVER = "rover"
ROLE_GROUND_STATION = "ground_station"


def new_start_sequence():
    try:
        data = os.urandom(2)
        boot_id = (data[0] << 8) | data[1]
    except Exception:
        boot_id = utime.ticks_ms() & 0xFFFF
    return boot_id * 1000


class EggNode:
    """
    Orchestrates one node in the LoRa/UWB mesh.
    Localisation logic lives in LocalisationController; this class handles
    packet routing, periodic tasks, and display.
    """

    def __init__(self, config, radio, uwb=None, thermistor=None, oled=None):
        self.config = config
        self.logger = NodeLogger()

        self.radio = radio
        self.uwb = uwb
        self.thermistor = thermistor
        self.oled = oled

        self.node_id = config.get("node_id", config.get("id", 0))
        self.node_name = config.get("node_name", "egg_{}".format(self.node_id))
        self.node_role = self._normalise_role(config.get("node_role", ROLE_FIELD_EGG))
        self.ground_station_id = config.get("ground_station_id", config.get("base_station_id", 0))
        if self.is_ground_station() and "ground_station_id" not in config and "base_station_id" not in config:
            self.ground_station_id = self.node_id

        self.uwb_id = config.get("uwb_id", self.node_id)
        self.uwb_channel = config.get("uwb_channel", 1)
        self.uwb_rate = config.get("uwb_rate", 1)

        self.heartbeat_interval_ms = config.get("heartbeat_interval_ms", 30000)
        self.sensor_interval_ms = config.get("sensor_interval_ms", 60000)
        self.range_interval_ms = config.get("range_interval_ms", 10000)
        self.display_interval_ms = config.get("display_interval_ms", 1000)
        self.repair_window_ms = config.get("repair_window_ms", 15000)
        self.seen_cache_ms = config.get("seen_cache_ms", 300000)
        self.default_ttl = config.get("default_ttl", 5)
        self.telemetry_enabled = config.get("telemetry_enabled", True)

        self.rover_localise_interval_ms = config.get("rover_localise_interval_ms", 10000)
        self.rover_localise_frames = config.get("rover_localise_frames", 8)
        self.rover_min_anchors = max(2, config.get("rover_min_anchors", 2))

        suspect_ms = config.get("neighbour_suspect_ms", 75000)
        lost_ms = config.get("neighbour_lost_ms", 120000)
        self.neighbours = NeighbourTable(suspect_ms, lost_ms)

        self.seq = new_start_sequence()
        self.seen_packets = {}

        self.started = False
        self.needs_repair = False
        self.repair_until = None
        self.rover_until = None
        self.uwb_ready = False  # used only by rover path

        self.position = None
        self.positions = {}
        self.node_directory = {
            self.node_id: {
                "node_id": self.node_id,
                "name": self.node_name,
                "uwb_id": self.uwb_id,
                "role": self.node_role,
            }
        }

        self.last_heartbeat = None
        self.last_sensor_report = None
        self.last_range_report = None
        self.last_display = None
        self.last_serial_status = None
        self.last_rover_localise = None
        self.last_rx = "-"
        self.last_tx = "-"

        # Build localisation controller only for field eggs with UWB enabled.
        # Set "localisation_pc_mode": true in config.json to use the simplified
        # PC-centralised controller (ranges and reports distances, no on-device MDS).
        localisation_enabled = config.get("localisation_enabled", True)
        pc_mode = config.get("localisation_pc_mode", False)
        _Controller = PCRangingController if pc_mode else LocalisationController
        if self.is_field_egg() and localisation_enabled and uwb is not None:
            self.localisation = _Controller(
                config=config,
                uwb=uwb,
                logger=self.logger,
                node_id=self.node_id,
                node_name=self.node_name,
                uwb_id=self.uwb_id,
                uwb_channel=self.uwb_channel,
                uwb_rate=self.uwb_rate,
                send_packet_fn=self.send_packet,
                next_seq_fn=self.next_seq,
                poll_radio_fn=self.poll_lora,
                neighbours=self.neighbours,
                node_positions=self.positions,
                node_directory=self.node_directory,
                set_self_pos_fn=self._set_position,
                on_complete_fn=self._on_localisation_complete,
            )
        else:
            self.localisation = None

        # Build packet dispatch table (avoids long if-elif chain in handle_packet)
        self._packet_handlers = {
            HELLO:              self.handle_hello,
            HEARTBEAT:          self.handle_heartbeat,
            SENSOR_REPORT:      self.handle_sensor_report,
            RANGE_REPORT:       self.handle_range_report,
            ROVER_START:        self.handle_rover_start,
            ROVER_STOP:         self.handle_rover_stop,
            IMAGE_OFFER:        self.handle_image_offer,
            LOCALISE_DISCOVERY: self.handle_localise_discovery,
            LOCALISE_START:     self.handle_localise_start,
            LOCALISE_TURN:      self.handle_localise_turn,
            LOCALISE_RESULT:    self.handle_localise_result,
            LOCALISE_POSITION:  self.handle_localise_position,
        }

    # ------------------------------------------------------------------ #
    # Role helpers                                                         #
    # ------------------------------------------------------------------ #

    def _normalise_role(self, role_name):
        if role_name in (ROLE_FIELD_EGG, ROLE_ROVER, ROLE_GROUND_STATION):
            return role_name
        return ROLE_FIELD_EGG

    def is_field_egg(self):
        return self.node_role == ROLE_FIELD_EGG

    def is_rover(self):
        return self.node_role == ROLE_ROVER

    def is_ground_station(self):
        return self.node_role == ROLE_GROUND_STATION

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def poll(self, now):
        self.poll_lora(now)

        if not self.started:
            self.started = True
            self.last_heartbeat = now
            self.send_hello(now)
            if self.localisation is not None:
                self.localisation.start(now, reason="boot")
            elif self.is_rover() and self.uwb is not None:
                self._set_uwb_mode(role=0, cold=True)

        # Always advance localisation while it is running.
        if self.localisation is not None and self.localisation.state not in ("idle", "steady"):
            self.localisation.advance(now)

        # Short-circuit the main loop only during active ranging/waiting —
        # not during settling, so heartbeats keep firing and HELLOs reach peers.
        loc_active = (
            self.localisation is not None
            and self.localisation.state in ("coordinator", "waiting_for_map")
        )
        if loc_active:
            if due(now, self.last_display, self.display_interval_ms):
                self.update_display(now)
            self.clean_seen_cache(now)
            gc.collect()
            return

        if due(now, self.last_heartbeat, self.heartbeat_interval_ms):
            self.send_heartbeat(now)

        if self.is_field_egg() and self.thermistor is not None and due(now, self.last_sensor_report, self.sensor_interval_ms):
            self.send_sensor_report(now)

        if (
            self.is_field_egg()
            and self.uwb is not None
            and self.rover_until is not None
            and due(now, self.last_range_report, self.range_interval_ms)
        ):
            self.send_range_report(now)

        if self.is_rover() and self.uwb is not None and due(now, self.last_rover_localise, self.rover_localise_interval_ms):
            self.localise_rover(now)

        if self.rover_until is not None and utime.ticks_diff(now, self.rover_until) >= 0:
            self.rover_until = None

        if self.is_field_egg() and self.neighbours.check_timeouts(now):
            unknown = [n["node_id"] for n in self.neighbours.records.values()
                       if n["node_id"] not in self.positions]
            if unknown:
                self.logger.event("REPAIR TRIGGER", [
                    ("Source", "timeout"),
                    ("Unknown neighbours", unknown),
                    ("Positions", list(self.positions.keys())),
                ])
                self.needs_repair = True

        if self.needs_repair:
            self.start_repair(now)

        if self.repair_until is not None and utime.ticks_diff(now, self.repair_until) >= 0:
            self.repair_until = None

        if due(now, self.last_display, self.display_interval_ms):
            self.update_display(now)

        self.clean_seen_cache(now)
        gc.collect()

    def poll_lora(self, now):
        if self.radio is None:
            return

        try:
            raw = self.radio.poll_receive()
        except Exception as exc:
            self.logger.event("LORA POLL ERROR", [("Error", exc)])
            return

        if raw is None:
            return

        # Binary protocol packets (image chunks, RDY signals) are not JSON — discard quietly.
        if isinstance(raw, (bytes, bytearray)):
            if raw[:3] in (b"IMG", b"RDY", b"ACK"):
                return
        elif isinstance(raw, str):
            if raw[:3] in ("IMG", "RDY", "ACK"):
                return

        packet = decode_packet(raw)
        if packet is None:
            self.logger.event("DROPPED PACKET", [("Reason", "not MVP format"), ("Raw", raw)])
            return

        self.handle_packet(packet, now, self.radio.last_rssi, self.radio.last_snr)

    # ------------------------------------------------------------------ #
    # Packet handling                                                      #
    # ------------------------------------------------------------------ #

    def handle_packet(self, packet, now, rssi=None, snr=None):
        src = packet.get("src")
        if src == self.node_id:
            return

        uid = packet_uid(packet)
        if uid in self.seen_packets:
            return
        self.seen_packets[uid] = now

        via = packet.get("via", src)
        if via is not None and via != self.node_id:
            changed = self.neighbours.update_seen(via, now, rssi, snr)
            if (changed
                    and self.is_field_egg()
                    and (self.localisation is None or self.localisation.state == "steady")
                    and via not in self.positions
                    and packet.get("t") not in LOCALISATION_PACKET_TYPES):
                self.logger.event("REPAIR TRIGGER", [
                    ("Source", "new packet"),
                    ("Via", via),
                    ("Positions", list(self.positions.keys())),
                ])
                self.needs_repair = True

        packet_type = packet.get("t")
        payload = packet.get("p", {})
        if packet_type in (HELLO, LOCALISE_DISCOVERY):
            self.remember_node(src, payload)

        self.last_rx = "{} from {}".format(packet_type, src)
        extra = []
        if packet_type == HEARTBEAT:
            extra.append(("status", payload.get("status", "ok")))
        if rssi is not None:
            extra.append(("RSSI", rssi))
        if snr is not None:
            extra.append(("SNR", snr))
        self.logger.packet("RX", packet, extra, compact=(packet_type == HEARTBEAT))

        if is_for_node(packet, self.node_id):
            handler = self._packet_handlers.get(packet_type)
            if handler:
                handler(packet, now, rssi=rssi, snr=snr)

        if self.should_relay(packet):
            self.relay_packet(packet)

    def handle_hello(self, packet, now, rssi=None, snr=None):
        payload = packet.get("p", {})
        self.logger.item("Name", payload.get("name", "-"))
        self.remember_position(packet.get("src"), payload)
        self.emit_map_for_payload(packet.get("src"), payload, rssi=rssi, snr=snr)

    def handle_heartbeat(self, packet, now, rssi=None, snr=None):
        payload = packet.get("p", {})
        self.remember_position(packet.get("src"), payload)
        self.emit_map_for_payload(packet.get("src"), payload, rssi=rssi, snr=snr)
        if payload.get("phase") == "steady":
            self.logger.item("Phase", "steady")

    def handle_sensor_report(self, packet, now, rssi=None, snr=None):
        self.logger.item("Reading", packet.get("p", {}))

    def handle_range_report(self, packet, now, rssi=None, snr=None):
        self.logger.item("Ranges", packet.get("p", {}))

    def handle_rover_start(self, packet, now, rssi=None, snr=None):
        seconds = packet.get("p", {}).get("seconds", 60)
        self.rover_until = utime.ticks_add(now, int(seconds) * 1000)
        self.logger.item("Rover", "started for {} seconds".format(seconds))

    def handle_rover_stop(self, packet, now, rssi=None, snr=None):
        self.rover_until = None
        self.logger.item("Rover", "stopped")

    def handle_image_offer(self, packet, now, rssi=None, snr=None):
        payload = packet.get("p", {})
        filename = payload.get("file", "received_image.bin")
        size = payload.get("size", 0)
        self.logger.event("IMAGE OFFER", [("File", filename), ("Size", size)])
        self._safe_display("IMG offer rx\nSending RDY...")

        def _progress(received, total):
            self.logger.event("IMAGE RX", [("Chunk", received), ("Total", total)])
            self._safe_display("Receiving IMG\n{}/{} chunks".format(received, total))

        result = self.radio.receive_image(output_path=filename, progress_cb=_progress)

        if result is not None:
            self.logger.event("IMAGE RECEIVED", [("File", result)])
            self._safe_display("IMG received!\nDisplaying...")
            if self.oled is not None:
                try:
                    self.oled.display_image(result)
                except Exception as exc:
                    self.logger.event("OLED DISPLAY ERROR", [("Error", exc)])
        else:
            self.logger.event("IMAGE RECEIVE FAILED", [])
            self._safe_display("IMG RX failed\nno chunks rx")

    # --- Localisation packet handlers — thin delegates to controller ---

    def handle_localise_discovery(self, packet, now, rssi=None, snr=None):
        if self.localisation is not None:
            self.localisation.handle_discovery(packet, now)

    def handle_localise_start(self, packet, now, rssi=None, snr=None):
        if self.localisation is not None:
            self.localisation.handle_start(packet, now)

    def handle_localise_turn(self, packet, now, rssi=None, snr=None):
        if self.localisation is not None:
            self.localisation.handle_turn(packet, now)

    def handle_localise_result(self, packet, now, rssi=None, snr=None):
        if self.localisation is not None:
            self.localisation.handle_result(packet, now)

    def handle_localise_position(self, packet, now, rssi=None, snr=None):
        """
        All node types store incoming positions (rovers need anchor positions).
        Telemetry emission happens here; state-machine update delegated to controller.
        """
        payload = packet.get("p", {})
        node_id = payload.get("node_id")
        if node_id is None:
            return
        try:
            pos = (
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                float(payload.get("z", 0.0)),
            )
        except Exception:
            return

        self.emit_map_telemetry(node_id, pos, rssi=rssi, snr=snr)
        self.logger.item("Position {}".format(node_id), "{:.2f}, {:.2f}".format(pos[0], pos[1]))

        if self.localisation is not None:
            # Field egg: controller updates positions dict + drives state machine
            self.localisation.handle_position(packet, now)
        else:
            # Rover / ground station: store directly for rover localisation
            self.positions[node_id] = pos
            if node_id == self.node_id:
                self.position = pos

    # ------------------------------------------------------------------ #
    # Sending                                                              #
    # ------------------------------------------------------------------ #

    def send_packet(self, packet):
        if self.radio is None:
            return False

        uid = packet_uid(packet)
        self.seen_packets[uid] = utime.ticks_ms()
        encoded = encode_packet(packet)

        try:
            self.radio.send(encoded)
            self.last_tx = "{} seq {}".format(packet.get("t"), packet.get("seq"))
            return True
        except Exception as exc:
            self.logger.event("LORA SEND ERROR", [("Error", exc)])
            return False

    def send_image(self, path, dst):
        """
        Broadcasts an IMAGE_OFFER then transfers the file in 240-byte chunks.
        Returns True if all chunks were acknowledged.
        """
        if self.radio is None:
            return False

        try:
            with open(path, "rb") as f:
                size = len(f.read())
        except OSError as exc:
            self.logger.event("IMAGE SEND ERROR", [("Error", exc)])
            return False

        if not self._wait_for_image_ready(path, dst, size):
            return False

        def _progress(sent, total, failed=False):
            if failed:
                self.logger.event("IMAGE TX FAILED", [("Chunk", sent), ("Total", total)])
                self._safe_display("IMG FAILED\nchunk {}/{}".format(sent, total))
            else:
                self.logger.event("IMAGE TX", [("Chunk", sent), ("Total", total)])
                self._safe_display("Sending IMG\n{}/{} chunks".format(sent, total))

        return self.radio.send_image(path, progress_cb=_progress)

    def _wait_for_image_ready(self, path, dst, size):
        """Broadcasts IMAGE_OFFER up to 10 times, returns True once RDY is received."""
        MAX_RETRIES = 10
        READY_TIMEOUT_MS = 6000

        for attempt in range(1, MAX_RETRIES + 1):
            self._safe_display("Offering IMG\n{}/{}".format(attempt, MAX_RETRIES))
            packet = make_packet(
                IMAGE_OFFER,
                self.node_id,
                dst,
                self.next_seq(),
                ttl=self.default_ttl,
                payload={"file": path, "size": size},
            )
            self.logger.event("IMAGE OFFER TX", [
                ("File", path), ("Size", size), ("Dst", dst), ("Attempt", attempt)])
            self.send_packet(packet)

            start = utime.ticks_ms()
            while utime.ticks_diff(utime.ticks_ms(), start) < READY_TIMEOUT_MS:
                raw = self.radio.poll_receive()
                if raw is not None:
                    data = raw if isinstance(raw, bytes) else raw.encode()
                    if data == b"RDY":
                        self.logger.event("IMAGE RDY RECEIVED", [("Attempt", attempt)])
                        self._safe_display("RDY received!\nSending IMG...")
                        return True
                    self.logger.event("IMAGE RDY WAIT RX", [("Got", repr(data[:20]))])
                utime.sleep_ms(10)

            self.logger.event("IMAGE OFFER NO RDY", [("Attempt", attempt)])
            utime.sleep_ms(urandom.randint(200, 1000))

        self.logger.event("IMAGE SEND ABORTED", [("Reason", "no RDY after retries")])
        self._safe_display("IMG ABORTED\nno RDY rx")
        return False

    def send_hello(self, now):
        payload = {
            "name": self.node_name,
            "role": self.node_role,
            "uwb_id": self.uwb_id,
        }
        if self.position is not None:
            payload["x"] = round(self.position[0], 4)
            payload["y"] = round(self.position[1], 4)
            payload["z"] = round(self.position[2], 4)

        packet = make_packet(
            HELLO,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=self.default_ttl,
            payload=payload,
        )
        self.logger.packet("TX", packet)
        self.send_packet(packet)

    def send_heartbeat(self, now):
        loc_state = self.localisation.state if self.localisation else "steady"
        packet = make_packet(
            HEARTBEAT,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=3,
            payload={
                "status": "ok",
                "phase": loc_state,
                "role": self.node_role,
            },
        )
        if self.position is not None:
            packet["p"]["x"] = round(self.position[0], 4)
            packet["p"]["y"] = round(self.position[1], 4)
            packet["p"]["z"] = round(self.position[2], 4)
        self.logger.packet("TX", packet, [("status", "ok")], compact=True)
        if self.send_packet(packet):
            self.last_heartbeat = now

    def send_sensor_report(self, now):
        self.last_sensor_report = now
        if self.thermistor is None:
            return
        try:
            reading = self.thermistor.read()
        except Exception as exc:
            self.logger.event("THERMISTOR READ ERROR", [("Error", exc)])
            return

        if self.position is not None:
            reading["x"] = round(self.position[0], 4)
            reading["y"] = round(self.position[1], 4)
            reading["z"] = round(self.position[2], 4)

        packet = make_packet(
            SENSOR_REPORT,
            self.node_id,
            self.ground_station_id,
            self.next_seq(),
            ttl=self.default_ttl,
            payload=reading,
        )
        self.logger.packet("TX", packet, [("Reading", reading)])
        self.send_packet(packet)

    def send_range_report(self, now):
        self.last_range_report = now
        if self.uwb is None:
            return
        try:
            distances = self.uwb.read_distance(timeout_ms=250)
        except Exception as exc:
            self.logger.event("UWB READ ERROR", [("Error", exc)])
            return

        if not distances:
            return

        packet = make_packet(
            RANGE_REPORT,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=3,
            payload={"d": distances},
        )
        self.logger.packet("TX", packet, [("Ranges", distances)])
        self.send_packet(packet)

    def send_rover_start(self, seconds=60):
        packet = make_packet(
            ROVER_START,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=self.default_ttl,
            payload={"seconds": int(seconds)},
        )
        self.logger.packet("TX", packet, [("seconds", seconds)], compact=True)
        self.send_packet(packet)

    def send_rover_stop(self):
        packet = make_packet(
            ROVER_STOP,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=self.default_ttl,
            payload={},
        )
        self.logger.packet("TX", packet, compact=True)
        self.send_packet(packet)

    # ------------------------------------------------------------------ #
    # Repair                                                               #
    # ------------------------------------------------------------------ #

    def start_repair(self, now):
        self.needs_repair = False
        self.repair_until = utime.ticks_add(now, self.repair_window_ms)
        if self.localisation is not None:
            self.logger.event("REPAIR REFRESH", [("Action", "re-localise")])
            self.localisation.start(now, reason="repair")
            return
        self.logger.event("REPAIR REFRESH", [("Action", "broadcast HELLO")])
        self.send_hello(now)

    # ------------------------------------------------------------------ #
    # Rover localisation                                                   #
    # ------------------------------------------------------------------ #

    def localise_rover(self, now):
        if not self.is_rover() or self.uwb is None:
            return

        self.last_rover_localise = now
        try:
            if not self._set_uwb_mode(role=0, cold=False):
                return
            self.uwb.flush()
            raw = self.uwb.scan(frames=self.rover_localise_frames)
        except Exception as exc:
            self.logger.event("ROVER LOCALISE ERROR", [("Error", exc)])
            return

        anchor_measurements = []
        for node_id, coords in self.positions.items():
            if node_id == self.node_id:
                continue
            info = self.node_directory.get(node_id, {})
            if info.get("role") != ROLE_FIELD_EGG:
                continue
            uwb_id = info.get("uwb_id")
            if uwb_id is None:
                continue
            distance = raw.get(uwb_id)
            if distance is None or distance <= 0:
                continue
            anchor_measurements.append((coords, float(distance)))

        if len(anchor_measurements) < self.rover_min_anchors:
            return

        from localisation.solver import solve_position
        result = solve_position(anchor_measurements, current_pos=self.position)
        if result is None:
            return

        self.position = (round(result[0], 4), round(result[1], 4), round(result[2], 4))
        self.positions[self.node_id] = self.position
        self.emit_map_telemetry(self.node_id, self.position)
        self.logger.event(
            "ROVER POSITION",
            [("X", self.position[0]), ("Y", self.position[1]), ("Anchors", len(anchor_measurements))],
        )

    # ------------------------------------------------------------------ #
    # Telemetry                                                            #
    # ------------------------------------------------------------------ #

    def emit_map_telemetry(self, node_id, position, rssi=0, snr=0.0):
        if not self.telemetry_enabled or position is None:
            return
        try:
            x, y, z = position
            message = {
                "type": "MAP",
                "id": int(node_id),
                "x": round(float(x), 4),
                "y": round(float(y), 4),
                "z": round(float(z), 4),
                "rssi": int(rssi or 0),
                "snr": round(float(snr or 0.0), 2),
                "role": self.node_directory.get(node_id, {}).get("role"),
                "name": self.node_directory.get(node_id, {}).get("name"),
                "via": self.node_id,
            }
            print(json.dumps(message))
        except Exception as exc:
            self.logger.event("TELEMETRY ERROR", [("Error", exc)])

    def emit_map_for_payload(self, node_id, payload, rssi=None, snr=None):
        if "x" not in payload or "y" not in payload:
            return
        try:
            position = (
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                float(payload.get("z", 0.0)),
            )
        except Exception:
            return
        self.emit_map_telemetry(node_id, position, rssi=rssi, snr=snr)

    # ------------------------------------------------------------------ #
    # Relay                                                                #
    # ------------------------------------------------------------------ #

    def relay_packet(self, packet):
        relayed = relay_copy(packet, self.node_id)
        if relayed is None:
            return
        self.logger.packet("RELAY", relayed, compact=True)
        self.send_packet(relayed)

    def should_relay(self, packet):
        return packet.get("t") not in LOCALISATION_PACKET_TYPES

    # ------------------------------------------------------------------ #
    # Node directory                                                       #
    # ------------------------------------------------------------------ #

    def remember_node(self, node_id, payload):
        entry = self.node_directory.get(node_id, {})
        entry["node_id"] = node_id
        entry["name"] = payload.get("name", entry.get("name", "egg_{}".format(node_id)))
        entry["uwb_id"] = payload.get("uwb_id", entry.get("uwb_id", node_id))
        entry["role"] = self._normalise_role(payload.get("role", entry.get("role", ROLE_FIELD_EGG)))
        self.node_directory[node_id] = entry

    def remember_position(self, node_id, payload):
        if node_id is None or "x" not in payload or "y" not in payload:
            return
        try:
            pos = (
                float(payload.get("x", 0.0)),
                float(payload.get("y", 0.0)),
                float(payload.get("z", 0.0)),
            )
        except Exception:
            return
        self.positions[node_id] = pos
        if node_id == self.node_id:
            self.position = pos

    # ------------------------------------------------------------------ #
    # Utilities                                                            #
    # ------------------------------------------------------------------ #

    def next_seq(self):
        self.seq += 1
        return self.seq

    def clean_seen_cache(self, now):
        old = [uid for uid, seen_at in self.seen_packets.items() if elapsed_ms(now, seen_at) > self.seen_cache_ms]
        for uid in old:
            del self.seen_packets[uid]

    def _set_position(self, pos):
        """Callback used by LocalisationController to update self.position."""
        self.position = pos

    def _on_localisation_complete(self, now):
        """Called by LocalisationController when localisation finishes."""
        if self.position is not None:
            self.emit_map_telemetry(self.node_id, self.position)
        self.send_hello(now)
        self.send_heartbeat(now)

    def _set_uwb_mode(self, role, cold=False):
        """UWB mode switch used by the rover path (field egg UWB is managed by the controller)."""
        if self.uwb is None:
            return False
        try:
            fn = self.uwb.configure if (cold or not self.uwb_ready) else self.uwb.configure_warm
            fn(self.uwb_id, role=role, channel=self.uwb_channel, rate=self.uwb_rate)
            self.uwb_ready = True
            return True
        except Exception as exc:
            self.logger.event("UWB CONFIG ERROR", [("Error", exc)])
            return False

    def _safe_display(self, text):
        if self.oled is not None:
            try:
                self.oled.display_text(text)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Display                                                              #
    # ------------------------------------------------------------------ #

    def state_label(self):
        if self.is_rover():
            return "ROVER"
        if self.is_ground_station():
            return "GROUND"
        if self.localisation is None:
            return "READY"
        s = self.localisation.state
        if s == "steady":
            return "READY"
        if s == "settling":
            return "SETTLE"
        if s == "waiting_for_map":
            return "WAIT"
        if s == "coordinator":
            return "COORD"
        return s.upper()

    def role_label(self):
        if self.is_field_egg():
            return "FIELD"
        if self.is_rover():
            return "ROVER"
        if self.is_ground_station():
            return "GROUND"
        return self.node_role.upper()

    def update_display(self, now):
        self.last_display = now
        alive, suspect, lost = self.neighbours.summary()
        line1 = "{} {}".format(self.role_label(), self.node_id)
        line2 = "{} A{} S{} L{}".format(self.state_label(), alive, suspect, lost)
        line3 = "XY -" if self.position is None else "XY {:.1f} {:.1f}".format(self.position[0], self.position[1])
        line4 = "RX {}".format(self.last_rx)
        text = "{}\n{}\n{}\n{}".format(line1, line2, line3, line4)

        if text != self.last_serial_status:
            self.last_serial_status = text
            items = [
                ("Node", "{} ({})".format(self.node_name, self.node_id)),
                ("Role", self.node_role),
                ("State", self.state_label()),
                ("Neighbours", "alive {} / suspect {} / lost {}".format(alive, suspect, lost)),
                ("Last RX", self.last_rx),
                ("Last TX", self.last_tx),
            ]
            if self.position is not None:
                items.append(("Position", "{:.2f}, {:.2f}".format(self.position[0], self.position[1])))
            self.logger.event("STATUS", items)

        self._safe_display(text)

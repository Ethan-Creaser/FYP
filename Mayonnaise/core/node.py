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

from localise import solve_from_distance_matrix, solve_position
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
    LOCALISE_POSITION,
    LOCALISE_RESULT,
    LOCALISE_TURN,
)

ROLE_FIELD_EGG = "field_egg"
ROLE_ROVER = "rover"
ROLE_GROUND_STATION = "ground_station"


def new_start_sequence():
    """
    Creates a random-ish starting packet sequence number for this boot.
    """
    try:
        data = os.urandom(2)
        boot_id = (data[0] << 8) | data[1]
    except Exception:
        boot_id = utime.ticks_ms() & 0xFFFF
    return boot_id * 1000


class EggNode:
    """
    Main controller for one egg node in the LoRa/UWB mesh.
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
        self.node_role = self.normalise_role(config.get("node_role", ROLE_FIELD_EGG))
        self.ground_station_id = config.get(
            "ground_station_id",
            config.get("base_station_id", 0),
        )
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

        self.localisation_enabled = config.get("localisation_enabled", True)
        self.localisation_boot_ms = config.get("localisation_boot_ms", 8000)
        self.localisation_announce_ms = config.get("localisation_announce_ms", 1500)
        self.localisation_turn_ms = config.get("localisation_turn_ms", 15000)
        self.localisation_frames = config.get("localisation_frames", 10)
        self.localisation_max_members = min(config.get("localisation_max_members", 8), 8)
        self.rover_localise_interval_ms = config.get("rover_localise_interval_ms", 10000)
        self.rover_localise_frames = config.get("rover_localise_frames", 8)
        self.rover_min_anchors = max(2, config.get("rover_min_anchors", 2))
        self.telemetry_enabled = config.get("telemetry_enabled", True)

        suspect_ms = config.get("neighbour_suspect_ms", 75000)
        lost_ms = config.get("neighbour_lost_ms", 120000)
        self.neighbours = NeighbourTable(suspect_ms, lost_ms)

        self.seq = new_start_sequence()
        self.seen_packets = {}

        self.started = False
        self.needs_repair = False
        self.repair_until = None
        self.rover_until = None
        self.uwb_ready = False

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

        self.localisation_state = "idle"
        self.localisation_complete = False
        self.localise_reason = None
        self.localise_deadline = None
        self.localise_last_announce = None
        self.localise_members = {}
        self.localise_coordinator = None
        self.localise_dist_matrix = {}
        self.localise_results = {}
        self.localise_expected_total = 0

        self.last_heartbeat = None
        self.last_sensor_report = None
        self.last_range_report = None
        self.last_display = None
        self.last_serial_status = None
        self.last_rover_localise = None
        self.last_rx = "-"
        self.last_tx = "-"

    def normalise_role(self, role_name):
        if role_name in (ROLE_FIELD_EGG, ROLE_ROVER, ROLE_GROUND_STATION):
            return role_name
        return ROLE_FIELD_EGG

    def is_field_egg(self):
        return self.node_role == ROLE_FIELD_EGG

    def is_rover(self):
        return self.node_role == ROLE_ROVER

    def is_ground_station(self):
        return self.node_role == ROLE_GROUND_STATION

    def poll(self, now):
        """
        Run one full polling-loop update for the node.
        """
        self.poll_lora(now)

        if not self.started:
            self.started = True
            self.last_heartbeat = now
            self.send_hello(now)
            if self.is_field_egg() and self.localisation_enabled and self.uwb is not None:
                self.start_localisation(now, reason="boot")
            else:
                self.localisation_state = "steady"
                self.localisation_complete = True
                if self.is_rover() and self.uwb is not None:
                    self.ensure_tag_mode(cold=True)

        if self.localisation_state != "steady":
            self.advance_localisation(now)
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
        """
        Check for one incoming LoRa packet without blocking the main loop.
        """
        if self.radio is None:
            return

        try:
            raw = self.radio.poll_receive()
        except Exception as exc:
            self.logger.event("LORA POLL ERROR", [("Error", exc)])
            return

        if raw is None:
            return

        # Raw binary protocol packets (IMG chunks, RDY signals) are not JSON — discard quietly.
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

    def handle_packet(self, packet, now, rssi=None, snr=None):
        """
        Process one decoded incoming packet and relay it if needed.
        """
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
            if changed and self.is_field_egg() and self.localisation_state == "steady":
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
            if packet_type == HELLO:
                self.handle_hello(packet, now, rssi=rssi, snr=snr)
            elif packet_type == HEARTBEAT:
                self.handle_heartbeat(packet, now, rssi=rssi, snr=snr)
            elif packet_type == SENSOR_REPORT:
                self.handle_sensor_report(packet, now)
            elif packet_type == RANGE_REPORT:
                self.handle_range_report(packet, now)
            elif packet_type == ROVER_START:
                self.handle_rover_start(packet, now)
            elif packet_type == ROVER_STOP:
                self.handle_rover_stop(packet, now)
            elif packet_type == LOCALISE_DISCOVERY:
                self.handle_localise_discovery(packet, now)
            elif packet_type == LOCALISE_TURN:
                self.handle_localise_turn(packet, now)
            elif packet_type == LOCALISE_RESULT:
                self.handle_localise_result(packet, now)
            elif packet_type == LOCALISE_POSITION:
                self.handle_localise_position(packet, now, rssi=rssi, snr=snr)
            elif packet_type == IMAGE_OFFER:
                self.handle_image_offer(packet, now)

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

    def handle_sensor_report(self, packet, now):
        self.logger.item("Reading", packet.get("p", {}))

    def handle_range_report(self, packet, now):
        self.logger.item("Ranges", packet.get("p", {}))

    def handle_rover_start(self, packet, now):
        seconds = packet.get("p", {}).get("seconds", 60)
        self.rover_until = utime.ticks_add(now, int(seconds) * 1000)
        self.logger.item("Rover", "started for {} seconds".format(seconds))

    def handle_rover_stop(self, packet, now):
        self.rover_until = None
        self.logger.item("Rover", "stopped")

    def handle_image_offer(self, packet, now):
        payload = packet.get("p", {})
        filename = payload.get("file", "received_image.bin")
        size = payload.get("size", 0)
        self.logger.event("IMAGE OFFER", [("File", filename), ("Size", size)])

        def _oled(text):
            if self.oled is not None:
                try:
                    self.oled.display_text(text)
                except Exception:
                    pass

        _oled("IMG offer rx\nSending RDY...")

        def _progress(received, total):
            self.logger.event("IMAGE RX", [("Chunk", received), ("Total", total)])
            _oled("Receiving IMG\n{}/{} chunks".format(received, total))

        result = self.radio.receive_image(output_path=filename, progress_cb=_progress)

        if result is not None:
            self.logger.event("IMAGE RECEIVED", [("File", result)])
            _oled("IMG received!\nDisplaying...")
            if self.oled is not None:
                try:
                    self.oled.display_image(result)
                except Exception as exc:
                    self.logger.event("OLED DISPLAY ERROR", [("Error", exc)])
        else:
            self.logger.event("IMAGE RECEIVE FAILED", [])
            _oled("IMG RX failed\nno chunks rx")

    def handle_localise_discovery(self, packet, now):
        if not self.is_field_egg():
            return

        if self.localisation_state not in ("discovering", "waiting_for_map"):
            return

        if packet.get("via") != packet.get("src"):
            return

        payload = packet.get("p", {})
        self.add_localise_member(
            packet.get("src"),
            payload.get("name"),
            payload.get("uwb_id"),
            now,
        )

    def handle_localise_turn(self, packet, now):
        if not self.is_field_egg():
            return

        payload = packet.get("p", {})
        coordinator = payload.get("coordinator")
        if coordinator is None:
            return

        self.localise_coordinator = coordinator
        self.logger.event(
            "LOCALISE TURN",
            [("Coordinator", coordinator), ("Node", self.node_id)],
        )
        members = payload.get("members", [])
        self.perform_localise_turn(members, coordinator, now)

    def handle_localise_result(self, packet, now):
        if not self.is_field_egg():
            return

        if self.localise_coordinator != self.node_id:
            return

        payload = packet.get("p", {})
        if payload.get("coordinator") != self.node_id:
            return

        source_id = packet.get("src")
        distances = payload.get("d", {})
        self.record_localise_result(source_id, distances, now)

    def handle_localise_position(self, packet, now, rssi=None, snr=None):
        payload = packet.get("p", {})
        coordinator = payload.get("coordinator")
        node_id = payload.get("node_id")
        if node_id is None:
            return

        if coordinator is not None and self.localise_coordinator is None:
            self.localise_coordinator = coordinator
        if (
            self.is_field_egg()
            and self.localisation_state == "waiting_for_map"
            and coordinator is not None
            and self.localise_coordinator is not None
            and coordinator != self.localise_coordinator
        ):
            return

        x_pos = float(payload.get("x", 0.0))
        y_pos = float(payload.get("y", 0.0))
        z_pos = float(payload.get("z", 0.0))
        self.positions[node_id] = (x_pos, y_pos, z_pos)
        if node_id == self.node_id:
            self.position = (x_pos, y_pos, z_pos)

        self.emit_map_telemetry(node_id, (x_pos, y_pos, z_pos), rssi=rssi, snr=snr)

        expected_total = payload.get("total")
        if expected_total is not None:
            self.localise_expected_total = int(expected_total)

        self.logger.item("Position {}".format(node_id), "{:.2f}, {:.2f}".format(x_pos, y_pos))

        if (
            self.localisation_state == "waiting_for_map"
            and self.localise_expected_total
            and len(self.positions) >= self.localise_expected_total
        ):
            self.finish_localisation(now, "map received")

    def send_packet(self, packet):
        """
        Encode and send one packet over LoRa.
        """
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
        '''
        announces and sends a binary image file to a specific node over LoRa.
        broadcasts an IMAGE_OFFER packet first so the destination enters receive
        mode, waits briefly, then transfers the file in 240-byte chunks with ACKs.
        inputs: path (str): local path to the .bin file
                dst (int): destination node id
        outputs: (bool) True if all chunks were acknowledged, False otherwise
        '''
        if self.radio is None:
            return False

        try:
            with open(path, "rb") as f:
                size = len(f.read())
        except OSError as exc:
            self.logger.event("IMAGE SEND ERROR", [("Error", exc)])
            return False

        packet = make_packet(
            IMAGE_OFFER,
            self.node_id,
            dst,
            self.next_seq(),
            ttl=self.default_ttl,
            payload={"file": path, "size": size},
        )
        MAX_OFFER_RETRIES = 10
        READY_TIMEOUT_MS  = 6000

        def _progress(sent, total, failed=False):
            if failed:
                self.logger.event("IMAGE TX FAILED", [("Chunk", sent), ("Total", total)])
            else:
                self.logger.event("IMAGE TX", [("Chunk", sent), ("Total", total)])
            if self.oled is None:
                return
            if failed:
                msg = "IMG FAILED\nchunk {}/{}".format(sent, total)
            else:
                msg = "Sending IMG\n{}/{} chunks".format(sent, total)
            try:
                self.oled.display_text(msg)
            except Exception:
                pass

        total_chunks = (size + 239) // 240

        def _oled_sender(text):
            if self.oled is not None:
                try:
                    self.oled.display_text(text)
                except Exception:
                    pass

        ready = False
        for attempt in range(1, MAX_OFFER_RETRIES + 1):
            _oled_sender("Offering IMG\n{}/{}".format(attempt, MAX_OFFER_RETRIES))
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
                        _oled_sender("RDY received!\nSending IMG...")
                        ready = True
                        break
                    self.logger.event("IMAGE RDY WAIT RX", [("Got", repr(data[:20]))])
                utime.sleep_ms(10)

            if ready:
                break
            self.logger.event("IMAGE OFFER NO RDY", [("Attempt", attempt)])
            jitter = urandom.randint(200, 1000)
            self.logger.event("IMAGE OFFER RETRY", [("JitterMs", jitter)])
            utime.sleep_ms(jitter)

        if not ready:
            self.logger.event("IMAGE SEND ABORTED", [("Reason", "no RDY after retries")])
            _oled_sender("IMG ABORTED\nno RDY rx")
            return False

        return self.radio.send_image(path, progress_cb=_progress)

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
        packet = make_packet(
            HEARTBEAT,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=3,
            payload={
                "status": "ok",
                "phase": self.localisation_state,
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

    def emit_map_telemetry(self, node_id, position, rssi=0, snr=0.0):
        if not self.telemetry_enabled or position is None:
            return

        try:
            x_pos, y_pos, z_pos = position
            message = {
                "type": "MAP",
                "id": int(node_id),
                "x": round(float(x_pos), 4),
                "y": round(float(y_pos), 4),
                "z": round(float(z_pos), 4),
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

    def relay_packet(self, packet):
        relayed = relay_copy(packet, self.node_id)
        if relayed is None:
            return

        self.logger.packet("RELAY", relayed, compact=True)
        self.send_packet(relayed)

    def should_relay(self, packet):
        return packet.get("t") not in LOCALISATION_PACKET_TYPES

    def start_repair(self, now):
        self.needs_repair = False
        self.repair_until = utime.ticks_add(now, self.repair_window_ms)
        if self.is_field_egg() and self.localisation_enabled and self.uwb is not None:
            self.logger.event("REPAIR REFRESH", [("Action", "re-localise")])
            self.start_localisation(now, reason="repair")
            return

        self.logger.event("REPAIR REFRESH", [("Action", "broadcast HELLO")])
        self.send_hello(now)

    def start_localisation(self, now, reason="boot"):
        if not self.is_field_egg() or not self.localisation_enabled or self.uwb is None:
            self.localisation_state = "steady"
            self.localisation_complete = True
            return

        self.localisation_state = "discovering"
        self.localisation_complete = False
        self.localise_reason = reason
        self.localise_deadline = utime.ticks_add(now, self.localisation_boot_ms)
        self.localise_last_announce = None
        self.localise_members = {}
        self.localise_coordinator = None
        self.localise_dist_matrix = {}
        self.localise_results = {}
        self.localise_expected_total = 0
        self.positions = {}
        self.add_localise_member(self.node_id, self.node_name, self.uwb_id, now)

        self.logger.event(
            "LOCALISATION START",
            [("Reason", reason), ("Node", self.node_id), ("UWB", self.uwb_id)],
        )
        self.ensure_anchor_mode(cold=(reason == "boot"))
        self.send_localise_discovery(now)

    def advance_localisation(self, now):
        if self.localisation_state == "discovering":
            if due(now, self.localise_last_announce, self.localisation_announce_ms):
                self.send_localise_discovery(now)

            if self.localise_deadline is not None and utime.ticks_diff(now, self.localise_deadline) >= 0:
                members = self.localise_member_list()
                if len(members) <= 1:
                    self.position = (0.0, 0.0, 0.0)
                    self.positions = {self.node_id: self.position}
                    self.finish_localisation(now, "solo node")
                    return

                self.localise_coordinator = min(member["node_id"] for member in members)
                if self.localise_coordinator == self.node_id:
                    self.run_localisation_round(now, members)
                else:
                    self.localisation_state = "waiting_for_map"
                    wait_budget = max(len(members), 2) * self.localisation_turn_ms
                    self.localise_deadline = utime.ticks_add(now, wait_budget)
                    self.logger.event(
                        "LOCALISATION FOLLOWER",
                        [("Coordinator", self.localise_coordinator), ("Members", self.member_id_text(members))],
                    )

        elif self.localisation_state == "waiting_for_map":
            if self.localise_deadline is not None and utime.ticks_diff(now, self.localise_deadline) >= 0:
                if self.position is not None:
                    self.finish_localisation(now, "map timeout with self position")
                else:
                    self.logger.event("LOCALISATION RETRY", [("Reason", "timeout waiting for map")])
                    self.start_localisation(now, reason="retry")

    def send_localise_discovery(self, now):
        packet = make_packet(
            LOCALISE_DISCOVERY,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=1,
            payload={
                "name": self.node_name,
                "role": self.node_role,
                "uwb_id": self.uwb_id,
            },
        )
        self.logger.packet("TX", packet, compact=True)
        self.send_packet(packet)
        self.localise_last_announce = now

    def run_localisation_round(self, now, members):
        self.localisation_state = "coordinator"
        self.localise_dist_matrix = {}
        self.localise_results = {}
        self.localise_expected_total = len(members)

        self.logger.event(
            "LOCALISATION COORDINATOR",
            [("Members", self.member_id_text(members))],
        )

        for member in members:
            target_id = member["node_id"]
            if target_id == self.node_id:
                distances = self.measure_localise_distances(members)
                self.record_localise_result(self.node_id, distances, utime.ticks_ms())
                gc.collect()
                continue

            packet = make_packet(
                LOCALISE_TURN,
                self.node_id,
                target_id,
                self.next_seq(),
                ttl=1,
                payload={
                    "coordinator": self.node_id,
                    "members": members,
                },
            )
            self.logger.packet("TX", packet, [("turn", target_id)], compact=True)
            self.send_packet(packet)

            deadline = utime.ticks_add(utime.ticks_ms(), self.localisation_turn_ms)
            while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
                loop_now = utime.ticks_ms()
                self.poll_lora(loop_now)
                if target_id in self.localise_results:
                    break
                utime.sleep_ms(50)
            else:
                self.logger.event("LOCALISATION TIMEOUT", [("Node", target_id)])

            gc.collect()

        node_ids = [member["node_id"] for member in members]
        try:
            positions = solve_from_distance_matrix(node_ids, self.localise_dist_matrix)
        except Exception as exc:
            self.logger.event("LOCALISATION SOLVE ERROR", [("Error", exc)])
            self.finish_localisation(utime.ticks_ms(), "solve failed")
            return

        if not positions:
            self.finish_localisation(utime.ticks_ms(), "no positions")
            return

        self.positions = {}
        for node_id, coords in positions.items():
            self.positions[node_id] = (
                round(coords[0], 4),
                round(coords[1], 4),
                round(coords[2], 4),
            )

        self.position = self.positions.get(self.node_id)
        self.broadcast_localisation_positions()
        self.finish_localisation(utime.ticks_ms(), "coordinator solved")

    def perform_localise_turn(self, members, coordinator, now):
        distances = self.measure_localise_distances(members)
        packet = make_packet(
            LOCALISE_RESULT,
            self.node_id,
            coordinator,
            self.next_seq(),
            ttl=1,
            payload={
                "coordinator": coordinator,
                "d": distances,
            },
        )
        self.logger.packet("TX", packet, [("Distances", distances)], compact=True)
        self.send_packet(packet)
        if self.localisation_state != "steady":
            self.localisation_state = "waiting_for_map"
            wait_budget = max(len(members), 2) * self.localisation_turn_ms
            self.localise_deadline = utime.ticks_add(now, wait_budget)

    def measure_localise_distances(self, members):
        if self.uwb is None:
            return {}

        distances = {}
        try:
            self.uwb.configure_warm(
                self.uwb_id,
                role=0,
                channel=self.uwb_channel,
                rate=self.uwb_rate,
            )
            self.uwb.flush()
            raw = self.uwb.scan(frames=self.localisation_frames)
        except Exception as exc:
            self.logger.event("LOCALISATION RANGE ERROR", [("Error", exc)])
            raw = {}
        finally:
            self.ensure_anchor_mode(cold=False)

        for member in members:
            other_id = member.get("node_id")
            other_uwb = member.get("uwb_id")
            if other_id == self.node_id or other_uwb is None:
                continue
            if other_uwb < 0 or other_uwb > 7:
                continue
            distance = raw.get(other_uwb)
            if distance is not None and distance > 0:
                distances[str(other_id)] = round(float(distance), 4)

        return distances

    def broadcast_localisation_positions(self):
        total = len(self.positions)
        for node_id in sorted(self.positions.keys()):
            x_pos, y_pos, z_pos = self.positions[node_id]
            packet = make_packet(
                LOCALISE_POSITION,
                self.node_id,
                BROADCAST,
                self.next_seq(),
                ttl=1,
                payload={
                    "coordinator": self.node_id,
                    "node_id": node_id,
                    "x": x_pos,
                    "y": y_pos,
                    "z": z_pos,
                    "total": total,
                },
            )
            self.logger.packet("TX", packet, [("Node", node_id)], compact=True)
            self.send_packet(packet)
            utime.sleep_ms(80)

    def record_localise_result(self, source_id, distances, now):
        self.localise_results[source_id] = True
        if source_id in self.localise_members:
            self.localise_members[source_id]["last_seen"] = now

        for target_key, distance in distances.items():
            try:
                target_id = int(target_key)
                distance_value = float(distance)
            except Exception:
                continue

            if target_id == source_id or distance_value <= 0:
                continue

            self.localise_dist_matrix[(source_id, target_id)] = distance_value
            self.neighbours.update_range(target_id, distance_value)

        self.logger.item("Localise result", "{} targets".format(len(distances)))

    def finish_localisation(self, now, reason):
        self.localisation_state = "steady"
        self.localisation_complete = True
        self.localise_deadline = None
        self.localise_last_announce = None

        items = [("Reason", reason)]
        if self.localise_coordinator is not None:
            items.append(("Coordinator", self.localise_coordinator))
        if self.position is not None:
            items.append(
                ("Position", "{:.2f}, {:.2f}".format(self.position[0], self.position[1]))
            )
        self.logger.event("LOCALISATION DONE", items)

        if self.position is not None:
            self.emit_map_telemetry(self.node_id, self.position)

        self.send_hello(now)
        self.send_heartbeat(now)

    def ensure_anchor_mode(self, cold=False):
        if self.uwb is None:
            return False

        try:
            if cold or not self.uwb_ready:
                self.uwb.configure(
                    self.uwb_id,
                    role=1,
                    channel=self.uwb_channel,
                    rate=self.uwb_rate,
                )
            else:
                self.uwb.configure_warm(
                    self.uwb_id,
                    role=1,
                    channel=self.uwb_channel,
                    rate=self.uwb_rate,
                )
            self.uwb_ready = True
            return True
        except Exception as exc:
            self.logger.event("UWB CONFIG ERROR", [("Error", exc)])
            return False

    def ensure_tag_mode(self, cold=False):
        if self.uwb is None:
            return False

        try:
            if cold or not self.uwb_ready:
                self.uwb.configure(
                    self.uwb_id,
                    role=0,
                    channel=self.uwb_channel,
                    rate=self.uwb_rate,
                )
            else:
                self.uwb.configure_warm(
                    self.uwb_id,
                    role=0,
                    channel=self.uwb_channel,
                    rate=self.uwb_rate,
                )
            self.uwb_ready = True
            return True
        except Exception as exc:
            self.logger.event("UWB TAG CONFIG ERROR", [("Error", exc)])
            return False

    def localise_rover(self, now):
        if not self.is_rover() or self.uwb is None:
            return

        self.last_rover_localise = now
        anchor_measurements = []
        try:
            if not self.ensure_tag_mode(cold=False):
                return
            self.uwb.flush()
            raw = self.uwb.scan(frames=self.rover_localise_frames)
        except Exception as exc:
            self.logger.event("ROVER LOCALISE ERROR", [("Error", exc)])
            return

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

        result = solve_position(anchor_measurements, current_pos=self.position)
        if result is None:
            return

        self.position = (
            round(result[0], 4),
            round(result[1], 4),
            round(result[2], 4),
        )
        self.positions[self.node_id] = self.position
        self.emit_map_telemetry(self.node_id, self.position)
        self.logger.event(
            "ROVER POSITION",
            [("X", self.position[0]), ("Y", self.position[1]), ("Anchors", len(anchor_measurements))],
        )

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

    def remember_node(self, node_id, payload):
        entry = self.node_directory.get(node_id, {})
        entry["node_id"] = node_id
        entry["name"] = payload.get("name", entry.get("name", "egg_{}".format(node_id)))
        entry["uwb_id"] = payload.get("uwb_id", entry.get("uwb_id", node_id))
        entry["role"] = self.normalise_role(payload.get("role", entry.get("role", ROLE_FIELD_EGG)))
        self.node_directory[node_id] = entry

    def remember_position(self, node_id, payload):
        if node_id is None:
            return
        if "x" not in payload or "y" not in payload:
            return

        try:
            x_pos = float(payload.get("x", 0.0))
            y_pos = float(payload.get("y", 0.0))
            z_pos = float(payload.get("z", 0.0))
        except Exception:
            return

        self.positions[node_id] = (x_pos, y_pos, z_pos)
        if node_id == self.node_id:
            self.position = (x_pos, y_pos, z_pos)

    def add_localise_member(self, node_id, name, uwb_id, now):
        if node_id is None or uwb_id is None:
            return

        self.node_directory[node_id] = {
            "node_id": node_id,
            "name": name or "egg_{}".format(node_id),
            "uwb_id": int(uwb_id),
            "role": ROLE_FIELD_EGG,
        }

        record = self.localise_members.get(node_id)
        if record is None:
            self.localise_members[node_id] = {
                "node_id": node_id,
                "name": name or "egg_{}".format(node_id),
                "uwb_id": int(uwb_id),
                "role": ROLE_FIELD_EGG,
                "last_seen": now,
            }
            self.logger.item("Discovery", "{} uwb {}".format(node_id, uwb_id))
            return

        record["last_seen"] = now
        record["name"] = name or record["name"]
        record["uwb_id"] = int(uwb_id)

    def localise_member_list(self):
        members = []
        for member in self.localise_members.values():
            uwb_id = member.get("uwb_id")
            if uwb_id is None or uwb_id < 0 or uwb_id > 7:
                continue
            members.append(
                {
                    "node_id": member["node_id"],
                    "name": member.get("name", "egg_{}".format(member["node_id"])),
                    "uwb_id": uwb_id,
                }
            )

        members.sort(key=lambda item: item["node_id"])
        if len(members) <= self.localisation_max_members:
            return members

        selected = members[:self.localisation_max_members]
        if not any(member["node_id"] == self.node_id for member in selected):
            selected[-1] = {
                "node_id": self.node_id,
                "name": self.node_name,
                "uwb_id": self.uwb_id,
            }
            selected.sort(key=lambda item: item["node_id"])

        self.logger.event(
            "LOCALISATION LIMIT",
            [("Using", self.member_id_text(selected)), ("Seen", self.member_id_text(members))],
        )
        return selected

    def member_id_text(self, members):
        return ",".join([str(member["node_id"]) for member in members])

    def next_seq(self):
        self.seq += 1
        return self.seq

    def clean_seen_cache(self, now):
        old = []
        for uid, seen_at in self.seen_packets.items():
            if elapsed_ms(now, seen_at) > self.seen_cache_ms:
                old.append(uid)

        for uid in old:
            del self.seen_packets[uid]

    def state_label(self):
        if self.is_rover():
            return "ROVER"
        if self.is_ground_station():
            return "GROUND"
        if self.localisation_state == "steady":
            return "READY"
        if self.localisation_state == "discovering":
            return "DISC"
        if self.localisation_state == "waiting_for_map":
            return "WAIT"
        if self.localisation_state == "coordinator":
            return "COORD"
        return self.localisation_state.upper()

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
        if self.position is None:
            line3 = "XY -"
        else:
            line3 = "XY {:.1f} {:.1f}".format(self.position[0], self.position[1])
        line4 = "RX {}".format(self.last_rx)
        text = "{}\n{}\n{}\n{}".format(line1, line2, line3, line4)

        if text != self.last_serial_status:
            self.last_serial_status = text
            items = [
                ("Node", "{} ({})".format(self.node_name, self.node_id)),
                ("Role", self.node_role),
                ("State", self.localisation_state),
                ("Neighbours", "alive {} / suspect {} / lost {}".format(alive, suspect, lost)),
                ("Last RX", self.last_rx),
                ("Last TX", self.last_tx),
            ]
            if self.position is not None:
                items.append(("Position", "{:.2f}, {:.2f}".format(self.position[0], self.position[1])))
            self.logger.event("STATUS", items)

        if self.oled is not None:
            try:
                self.oled.display_text(text)
            except Exception as exc:
                self.logger.event("OLED DISPLAY ERROR", [("Error", exc)])

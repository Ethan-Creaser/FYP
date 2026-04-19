import gc
import utime

from neighbour_table import NeighbourTable
from timers import due, elapsed_ms
from packets import (
    BROADCAST,
    HELLO,
    HEARTBEAT,
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


class EggNode:
    def __init__(self, config, radio, uwb=None, thermistor=None, oled=None):
        self.config = config
        self.radio = radio
        self.uwb = uwb
        self.thermistor = thermistor
        self.oled = oled

        self.node_id = config.get("node_id", config.get("id", 0))
        self.node_name = config.get("node_name", "egg_{}".format(self.node_id))
        self.base_station_id = config.get("base_station_id", 0)

        self.heartbeat_interval_ms = config.get("heartbeat_interval_ms", 30000)
        self.sensor_interval_ms = config.get("sensor_interval_ms", 60000)
        self.range_interval_ms = config.get("range_interval_ms", 10000)
        self.display_interval_ms = config.get("display_interval_ms", 1000)
        self.repair_window_ms = config.get("repair_window_ms", 15000)
        self.seen_cache_ms = config.get("seen_cache_ms", 300000)
        self.default_ttl = config.get("default_ttl", 5)

        suspect_ms = config.get("neighbour_suspect_ms", 75000)
        lost_ms = config.get("neighbour_lost_ms", 120000)
        self.neighbours = NeighbourTable(suspect_ms, lost_ms)

        self.seq = 0
        self.seen_packets = {}

        self.started = False
        self.needs_repair = False
        self.repair_until = None
        self.rover_until = None

        self.last_heartbeat = None
        self.last_sensor_report = None
        self.last_range_report = None
        self.last_display = None
        self.last_rx = "-"
        self.last_tx = "-"

    def poll(self, now):
        self.poll_lora(now)

        if not self.started:
            self.send_hello(now)
            self.started = True
            self.last_heartbeat = now

        if due(now, self.last_heartbeat, self.heartbeat_interval_ms):
            self.send_heartbeat(now)

        if self.thermistor is not None and due(now, self.last_sensor_report, self.sensor_interval_ms):
            self.send_sensor_report(now)

        if self.uwb is not None and due(now, self.last_range_report, self.range_interval_ms):
            self.send_range_report(now)

        if self.neighbours.check_timeouts(now):
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
            print("LoRa poll error:", exc)
            return

        if raw is None:
            return

        packet = decode_packet(raw)
        if packet is None:
            print("Dropped non-MVP packet:", raw)
            return

        self.handle_packet(packet, now, self.radio.last_rssi, self.radio.last_snr)

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
            if changed:
                self.needs_repair = True

        packet_type = packet.get("t")
        self.last_rx = "{} from {}".format(packet_type, src)
        print("RX {} from {} via {} ttl={}".format(packet_type, src, via, packet.get("ttl")))

        if is_for_node(packet, self.node_id):
            if packet_type == HELLO:
                self.handle_hello(packet, now)
            elif packet_type == HEARTBEAT:
                self.handle_heartbeat(packet, now)
            elif packet_type == SENSOR_REPORT:
                self.handle_sensor_report(packet, now)
            elif packet_type == RANGE_REPORT:
                self.handle_range_report(packet, now)
            elif packet_type == ROVER_START:
                self.handle_rover_start(packet, now)
            elif packet_type == ROVER_STOP:
                self.handle_rover_stop(packet, now)

        self.relay_packet(packet)

    def handle_hello(self, packet, now):
        print("HELLO from egg {}".format(packet.get("src")))

    def handle_heartbeat(self, packet, now):
        payload = packet.get("p", {})
        print("Heartbeat from egg {} status={}".format(packet.get("src"), payload.get("status", "ok")))

    def handle_sensor_report(self, packet, now):
        print("Sensor report from egg {}: {}".format(packet.get("src"), packet.get("p", {})))

    def handle_range_report(self, packet, now):
        print("Range report from egg {}: {}".format(packet.get("src"), packet.get("p", {})))

    def handle_rover_start(self, packet, now):
        seconds = packet.get("p", {}).get("seconds", 60)
        self.rover_until = utime.ticks_add(now, int(seconds) * 1000)
        print("Rover assist started for {} seconds".format(seconds))

    def handle_rover_stop(self, packet, now):
        self.rover_until = None
        print("Rover assist stopped")

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
            print("LoRa send error:", exc)
            return False

    def send_hello(self, now):
        packet = make_packet(
            HELLO,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=self.default_ttl,
            payload={"name": self.node_name},
        )
        print("TX HELLO")
        self.send_packet(packet)

    def send_heartbeat(self, now):
        packet = make_packet(
            HEARTBEAT,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=3,
            payload={"status": "ok"},
        )
        print("TX HEARTBEAT")
        if self.send_packet(packet):
            self.last_heartbeat = now

    def send_sensor_report(self, now):
        self.last_sensor_report = now
        if self.thermistor is None:
            return

        try:
            reading = self.thermistor.read()
        except Exception as exc:
            print("Thermistor read error:", exc)
            return

        packet = make_packet(
            SENSOR_REPORT,
            self.node_id,
            self.base_station_id,
            self.next_seq(),
            ttl=self.default_ttl,
            payload=reading,
        )
        print("TX SENSOR_REPORT {}".format(reading))
        self.send_packet(packet)

    def send_range_report(self, now):
        self.last_range_report = now
        if self.uwb is None:
            return

        try:
            distances = self.uwb.read_distance(timeout_ms=50)
        except Exception as exc:
            print("UWB read error:", exc)
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
        print("TX RANGE_REPORT")
        self.send_packet(packet)

    def relay_packet(self, packet):
        relayed = relay_copy(packet, self.node_id)
        if relayed is None:
            return

        print("Relay {} from {} ttl={}".format(
            relayed.get("t"), relayed.get("src"), relayed.get("ttl")
        ))
        self.send_packet(relayed)

    def start_repair(self, now):
        self.needs_repair = False
        self.repair_until = utime.ticks_add(now, self.repair_window_ms)
        print("Repair refresh: broadcasting HELLO")
        self.send_hello(now)

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

    def update_display(self, now):
        self.last_display = now
        alive, suspect, lost = self.neighbours.summary()
        line1 = "{} id {}".format(self.node_name, self.node_id)
        line2 = "N A{} S{} L{}".format(alive, suspect, lost)
        line3 = "RX {}".format(self.last_rx)
        line4 = "TX {}".format(self.last_tx)
        text = "{}\n{}\n{}\n{}".format(line1, line2, line3, line4)

        print(text.replace("\n", " | "))
        if self.oled is not None:
            try:
                self.oled.display_text(text)
            except Exception as exc:
                print("OLED display error:", exc)

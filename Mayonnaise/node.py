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


LOG_WIDTH = 48


class EggNode:
    '''
    main controller for one egg node in the LoRa/UWB mesh MVP
    inputs: config (dict), radio (LoRaTransceiver or None), uwb (BU03 or None),
            thermistor (Thermistor or None), oled (OLED or None)
    outputs: EggNode object that can be repeatedly updated by calling poll(now)
    '''

    def __init__(self, config, radio, uwb=None, thermistor=None, oled=None):
        '''
        creates the egg node and stores all runtime state used by the polling loop
        inputs: config (dict), radio (LoRaTransceiver or None), uwb (BU03 or None),
                thermistor (Thermistor or None), oled (OLED or None)
        outputs: none
        '''
        self.config = config

        # stores its hardware objects
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
        self.last_serial_status = None
        self.last_rx = "-"
        self.last_tx = "-"

    def log_event(self, title, items=None):
        '''
        prints a formatted multi-line event block to the serial console
        inputs: title (str), items (list of label/value tuples or None)
        outputs: none
        '''
        print("")
        print("-" * LOG_WIDTH)
        print(title)
        print("-" * LOG_WIDTH)
        if items:
            for label, value in items:
                self.log_item(label, value)

    def log_item(self, label, value):
        '''
        prints one formatted label/value line to the serial console
        inputs: label (str), value (any printable value)
        outputs: none
        '''
        print("  {:<14} {}".format(label + ":", value))

    def node_label(self, node_id):
        '''
        converts a node id into a human-readable label for logs
        inputs: node_id (int or None): node id, or None for broadcast packets
        outputs: (str) readable node label
        '''
        if node_id is BROADCAST:
            return "broadcast"
        return "egg {}".format(node_id)

    def log_packet(self, direction, packet, extra=None, compact=False):
        '''
        prints a packet in either compact or detailed serial log format
        inputs: direction (str), packet (dict), extra (list of label/value tuples or None),
                compact (bool): true for one-line logs
        outputs: none
        '''
        packet_type = packet.get("t", "PACKET")
        if compact:
            details = [
                "src={}".format(self.node_label(packet.get("src"))),
                "to={}".format(self.node_label(packet.get("dst"))),
                "via={}".format(self.node_label(packet.get("via"))),
                "seq={}".format(packet.get("seq")),
                "ttl={}".format(packet.get("ttl")),
            ]
            if extra:
                for label, value in extra:
                    details.append("{}={}".format(label.lower(), value))
            print("[{} {}] {}".format(direction, packet_type, " ".join(details)))
            return

        items = [
            ("Source", self.node_label(packet.get("src"))),
            ("To", self.node_label(packet.get("dst"))),
            ("Via", self.node_label(packet.get("via"))),
            ("Seq", packet.get("seq")),
            ("TTL", packet.get("ttl")),
        ]
        if extra:
            items.extend(extra)
        self.log_event("{} {}".format(direction, packet.get("t", "PACKET")), items)

    def poll(self, now):
        '''
        runs one full polling-loop update for the node
        inputs: now (int): current MicroPython time from utime.ticks_ms()
        outputs: none
        '''
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
        '''
        checks for one incoming LoRa packet without blocking the main loop
        inputs: now (int): current MicroPython time from utime.ticks_ms()
        outputs: none
        '''
        if self.radio is None:
            return

        try:
            raw = self.radio.poll_receive()
        except Exception as exc:
            self.log_event("LORA POLL ERROR", [("Error", exc)])
            return

        if raw is None:
            return

        packet = decode_packet(raw)
        if packet is None:
            self.log_event("DROPPED PACKET", [("Reason", "not MVP format"), ("Raw", raw)])
            return

        self.handle_packet(packet, now, self.radio.last_rssi, self.radio.last_snr)

    def handle_packet(self, packet, now, rssi=None, snr=None):
        '''
        processes one decoded incoming packet and relays it if needed
        inputs: packet (dict), now (int), rssi (int/float or None), snr (int/float or None)
        outputs: none
        '''
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
        extra = []
        if packet_type == HEARTBEAT:
            extra.append(("status", packet.get("p", {}).get("status", "ok")))
        if rssi is not None:
            extra.append(("RSSI", rssi))
        if snr is not None:
            extra.append(("SNR", snr))
        self.log_packet("RX", packet, extra, compact=(packet_type == HEARTBEAT))

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
        '''
        handles a HELLO packet from another egg
        inputs: packet (dict), now (int): current MicroPython time
        outputs: none
        '''
        payload = packet.get("p", {})
        self.log_item("Name", payload.get("name", "-"))

    def handle_heartbeat(self, packet, now):
        '''
        handles a HEARTBEAT packet from another egg
        inputs: packet (dict), now (int): current MicroPython time
        outputs: none
        '''
        pass

    def handle_sensor_report(self, packet, now):
        '''
        handles a SENSOR_REPORT packet from another egg
        inputs: packet (dict), now (int): current MicroPython time
        outputs: none
        '''
        self.log_item("Reading", packet.get("p", {}))

    def handle_range_report(self, packet, now):
        '''
        handles a RANGE_REPORT packet from another egg
        inputs: packet (dict), now (int): current MicroPython time
        outputs: none
        '''
        self.log_item("Ranges", packet.get("p", {}))

    def handle_rover_start(self, packet, now):
        '''
        handles a ROVER_START packet and enables rover assist for a fixed time
        inputs: packet (dict), now (int): current MicroPython time
        outputs: none
        '''
        seconds = packet.get("p", {}).get("seconds", 60)
        self.rover_until = utime.ticks_add(now, int(seconds) * 1000)
        self.log_item("Rover", "started for {} seconds".format(seconds))

    def handle_rover_stop(self, packet, now):
        '''
        handles a ROVER_STOP packet and disables rover assist
        inputs: packet (dict), now (int): current MicroPython time
        outputs: none
        '''
        self.rover_until = None
        self.log_item("Rover", "stopped")

    def send_packet(self, packet):
        '''
        encodes and sends one packet over LoRa
        inputs: packet (dict): packet created by make_packet or relay_copy
        outputs: (bool) true if send succeeds, false if radio is missing or send fails
        '''
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
            self.log_event("LORA SEND ERROR", [("Error", exc)])
            return False

    def send_hello(self, now):
        '''
        broadcasts a HELLO packet announcing this egg to nearby nodes
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        packet = make_packet(
            HELLO,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=self.default_ttl,
            payload={"name": self.node_name},
        )
        self.log_packet("TX", packet)
        self.send_packet(packet)

    def send_heartbeat(self, now):
        '''
        broadcasts a HEARTBEAT packet showing this egg is still alive
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        packet = make_packet(
            HEARTBEAT,
            self.node_id,
            BROADCAST,
            self.next_seq(),
            ttl=3,
            payload={"status": "ok"},
        )
        self.log_packet("TX", packet, [("status", "ok")], compact=True)
        if self.send_packet(packet):
            self.last_heartbeat = now

    def send_sensor_report(self, now):
        '''
        reads the thermistor and sends a SENSOR_REPORT packet to the base station
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        self.last_sensor_report = now
        if self.thermistor is None:
            return

        try:
            reading = self.thermistor.read()
        except Exception as exc:
            self.log_event("THERMISTOR READ ERROR", [("Error", exc)])
            return

        packet = make_packet(
            SENSOR_REPORT,
            self.node_id,
            self.base_station_id,
            self.next_seq(),
            ttl=self.default_ttl,
            payload=reading,
        )
        self.log_packet("TX", packet, [("Reading", reading)])
        self.send_packet(packet)

    def send_range_report(self, now):
        '''
        reads UWB distances and broadcasts a RANGE_REPORT packet if data is available
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        self.last_range_report = now
        if self.uwb is None:
            return

        try:
            distances = self.uwb.read_distance(timeout_ms=50)
        except Exception as exc:
            self.log_event("UWB READ ERROR", [("Error", exc)])
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
        self.log_packet("TX", packet, [("Ranges", distances)])
        self.send_packet(packet)

    def relay_packet(self, packet):
        '''
        forwards a packet by reducing its TTL and setting this egg as the via node
        inputs: packet (dict): packet received from another node
        outputs: none
        '''
        relayed = relay_copy(packet, self.node_id)
        if relayed is None:
            return

        self.log_packet("RELAY", relayed, compact=True)
        self.send_packet(relayed)

    def start_repair(self, now):
        '''
        starts a simple repair refresh by broadcasting a new HELLO packet
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        self.needs_repair = False
        self.repair_until = utime.ticks_add(now, self.repair_window_ms)
        self.log_event("REPAIR REFRESH", [("Action", "broadcast HELLO")])
        self.send_hello(now)

    def next_seq(self):
        '''
        increments and returns this node's next packet sequence number
        inputs: none
        outputs: (int) next sequence number
        '''
        self.seq += 1
        return self.seq

    def clean_seen_cache(self, now):
        '''
        removes old packet ids from the duplicate-detection cache
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        old = []
        for uid, seen_at in self.seen_packets.items():
            if elapsed_ms(now, seen_at) > self.seen_cache_ms:
                old.append(uid)

        for uid in old:
            del self.seen_packets[uid]

    def update_display(self, now):
        '''
        updates the OLED and prints node status if the serial status changed
        inputs: now (int): current MicroPython time
        outputs: none
        '''
        self.last_display = now
        alive, suspect, lost = self.neighbours.summary()
        line1 = "{} id {}".format(self.node_name, self.node_id)
        line2 = "N A{} S{} L{}".format(alive, suspect, lost)
        line3 = "RX {}".format(self.last_rx)
        line4 = "TX {}".format(self.last_tx)
        text = "{}\n{}\n{}\n{}".format(line1, line2, line3, line4)

        if text != self.last_serial_status:
            self.last_serial_status = text
            self.log_event("STATUS", [
                ("Node", "{} ({})".format(self.node_name, self.node_id)),
                ("Neighbours", "alive {} / suspect {} / lost {}".format(alive, suspect, lost)),
                ("Last RX", self.last_rx),
                ("Last TX", self.last_tx),
            ])

        if self.oled is not None:
            try:
                self.oled.display_text(text)
            except Exception as exc:
                self.log_event("OLED DISPLAY ERROR", [("Error", exc)])

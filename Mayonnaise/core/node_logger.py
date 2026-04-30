from packets import BROADCAST


LOG_WIDTH = 48


class NodeLogger:
    '''
    formats serial log messages for an egg node
    inputs: none
    outputs: NodeLogger object used by EggNode
    '''

    def __init__(self):
        self._outputs = []  # list of objects with a .log(line) method (WiFiLogger, BtLogger, …)

    def add_output(self, output):
        self._outputs.append(output)

    def _log(self, line):
        print(line)
        for out in self._outputs:
            try:
                out.log(line)
            except Exception:
                pass

    def event(self, title, items=None):
        '''
        prints a formatted multi-line event block to the serial console
        inputs: title (str), items (list of label/value tuples or None)
        outputs: none
        '''
        self._log("")
        self._log("-" * LOG_WIDTH)
        self._log(title)
        self._log("-" * LOG_WIDTH)
        if items:
            for label, value in items:
                self.item(label, value)

    def item(self, label, value):
        '''
        prints one formatted label/value line to the serial console
        inputs: label (str), value (any printable value)
        outputs: none
        '''
        self._log("  {:<14} {}".format(label + ":", value))

    def node_label(self, node_id):
        '''
        converts a node id into a human-readable label for logs
        inputs: node_id (int or None): node id, or None for broadcast packets
        outputs: (str) readable node label
        '''
        if node_id is BROADCAST:
            return "broadcast"
        return "egg {}".format(node_id)

    def packet(self, direction, packet, extra=None, compact=False):
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
            self._log("[{} {}] {}".format(direction, packet_type, " ".join(details)))
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
        self.event("{} {}".format(direction, packet.get("t", "PACKET")), items)

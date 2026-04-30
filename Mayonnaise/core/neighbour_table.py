from timers import elapsed_ms


class NeighbourTable:
    def __init__(self, suspect_ms=75000, lost_ms=120000):
        self.records = {}
        self.suspect_ms = suspect_ms
        self.lost_ms = lost_ms

    def update_seen(self, node_id, now, rssi=None, snr=None):
        record = self.records.get(node_id)
        is_new = record is None

        if record is None:
            record = {
                "node_id": node_id,
                "first_seen": now,
                "last_seen": now,
                "rssi": None,
                "snr": None,
                "status": "alive",
                "uwb_range": None,
            }
            self.records[node_id] = record

        old_status = record.get("status")
        record["last_seen"] = now
        record["status"] = "alive"

        if rssi is not None:
            record["rssi"] = rssi
        if snr is not None:
            record["snr"] = snr

        return is_new or old_status != "alive"

    def update_range(self, node_id, distance_m):
        record = self.records.get(node_id)
        if record is None:
            return
        record["uwb_range"] = distance_m

    def check_timeouts(self, now):
        changed = False
        for record in self.records.values():
            age = elapsed_ms(now, record["last_seen"])
            old_status = record["status"]

            if age >= self.lost_ms:
                record["status"] = "lost"
            elif age >= self.suspect_ms:
                record["status"] = "suspect"
            else:
                record["status"] = "alive"

            if record["status"] != old_status:
                changed = True

        return changed

    def alive(self):
        return [r for r in self.records.values() if r["status"] == "alive"]

    def count_alive(self):
        return len(self.alive())

    def summary(self):
        alive = 0
        suspect = 0
        lost = 0
        for record in self.records.values():
            status = record["status"]
            if status == "alive":
                alive += 1
            elif status == "suspect":
                suspect += 1
            elif status == "lost":
                lost += 1
        return alive, suspect, lost

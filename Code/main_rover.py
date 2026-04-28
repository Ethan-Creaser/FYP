"""
main_rover.py — Rover Node Firmware
=====================================
Upload as main.py to the rover board (laptop-connected node).
config.json: {"id": 99, "name": "rover"}  ← any ID not used by fixed nodes

The rover:
  - Listens passively on LoRa for MAP/OFFLINE messages from fixed nodes
  - Forwards everything to laptop via USB serial as JSON
  - Also localises itself relative to the fixed network when in range
  - Does NOT coordinate ranging or act as an anchor
  - Can be moving — positions update whenever it hears a broadcast

The laptop server.py works exactly the same — reads JSON from serial.
Rover appears on the map as a moving node alongside the fixed nodes.
"""

import ujson, utime, math, gc
from machine import Pin, SPI
from bu03  import BU03
from comms import Comms

PIN_I2C_SDA = 8
PIN_I2C_SCL = 9

# How often to request positions from fixed network
REQUEST_INTERVAL_MS  = 10_000
# How often to self-localise (rover position update)
LOCALISE_INTERVAL_MS = 20_000
# Minimum anchors needed to self-localise
MIN_ANCHORS = 2


def emit(obj):
    """Print JSON to USB serial → laptop server."""
    print(ujson.dumps(obj))


def init_oled():
    try:
        from Drivers.oled.oled_class import OLED
        o = OLED(sda=PIN_I2C_SDA, scl=PIN_I2C_SCL)
        o.display_text("ROVER\nBooting...")
        return o
    except Exception as e:
        print("[INIT] OLED FAILED:", e)
        return None


def _circle_intersect(p0, d0, p1, d1):
    x1,y1=p0[0],p0[1]; x2,y2=p1[0],p1[1]
    dx,dy=x2-x1,y2-y1
    D=math.sqrt(dx*dx+dy*dy)
    if D<1e-6: return None
    d0=min(d0,D+d1-1e-6); d1=min(d1,D+d0-1e-6)
    d0=max(d0,abs(D-d1)+1e-6)
    a=(d0*d0-d1*d1+D*D)/(2*D)
    h2=d0*d0-a*a
    if h2<0: h2=0.0
    h=math.sqrt(h2)
    mx=x1+a*dx/D; my=y1+a*dy/D
    px1,py1=mx+h*dy/D,my-h*dx/D
    px2,py2=mx-h*dy/D,my+h*dx/D
    return (px2,py2) if py2>=py1 else (px1,py1)


def _trilaterate(measurements):
    """Simple trilateration from anchor positions + distances."""
    n = len(measurements)
    if n == 0: return None
    if n == 1:
        p,d = measurements[0]
        return (p[0]+d, p[1], 0.0)
    if n == 2:
        (p0,d0),(p1,d1) = measurements
        r = _circle_intersect((p0[0],p0[1]),d0,(p1[0],p1[1]),d1)
        return (r[0],r[1],0.0) if r else (p0[0]+d0,p0[1],0.0)
    # 3+ — least squares
    (p0,d0)=measurements[0]; x0,y0=p0[0],p0[1]
    A_rows,b_rows=[],[]
    for (pi,di) in measurements[1:]:
        xi,yi=pi[0],pi[1]
        A_rows.append([2*(xi-x0),2*(yi-y0)])
        b_rows.append(d0*d0-di*di-x0*x0+xi*xi-y0*y0+yi*yi)
    AtA=[[0.0]*2 for _ in range(2)]; Atb=[0.0]*2
    for row,b in zip(A_rows,b_rows):
        for i in range(2):
            Atb[i]+=row[i]*b
            for j in range(2): AtA[i][j]+=row[i]*row[j]
    det=AtA[0][0]*AtA[1][1]-AtA[0][1]*AtA[1][0]
    if abs(det)<1e-9: return (_circle_intersect(
        (measurements[0][0][0],measurements[0][0][1]),measurements[0][1],
        (measurements[1][0][0],measurements[1][0][1]),measurements[1][1]) or
        (measurements[0][0][0]+measurements[0][1],measurements[0][0][1],0.0))
    x=(Atb[0]*AtA[1][1]-Atb[1]*AtA[0][1])/det
    y=(AtA[0][0]*Atb[1]-AtA[1][0]*Atb[0])/det
    return (x,y,0.0)


class Rover:

    def __init__(self, rover_id, comms, uwb, oled):
        self.rover_id       = rover_id
        self.comms          = comms
        self.uwb            = uwb
        self.oled           = oled
        self.anchor_map     = {}   # id → {x,y,z} — fixed node positions
        self.coords         = None # rover's own position
        self._last_request  = 0
        self._last_localise = 0
        self._last_oled     = 0

    def run(self):
        print("[Rover {}] Started — passive listener".format(self.rover_id))
        self._oled("ROVER\nListening...")

        # Configure UWB as tag — rover measures, never anchors
        self.uwb.configure(self.rover_id, role=0)

        # Immediately request positions from fixed network
        self.comms.send({"type":"REQUEST_MAP","id":self.rover_id})

        while True:
            now = utime.ticks_ms()

            # Listen for LoRa messages from fixed network
            msg = self.comms.recv()
            if msg:
                self._handle(msg)

            # Periodically request fresh positions
            if utime.ticks_diff(now, self._last_request) >= REQUEST_INTERVAL_MS:
                self.comms.send({"type":"REQUEST_MAP","id":self.rover_id})
                self._last_request = utime.ticks_ms()

            # Periodically self-localise
            if (len(self.anchor_map) >= MIN_ANCHORS and
                    utime.ticks_diff(now, self._last_localise) >= LOCALISE_INTERVAL_MS):
                self._self_localise()
                self._last_localise = utime.ticks_ms()

            if utime.ticks_diff(now, self._last_oled) >= 2000:
                self._refresh_oled()
                self._last_oled = utime.ticks_ms()

            gc.collect()
            utime.sleep_ms(50)

    def _handle(self, msg):
        t = msg.get("type")

        if t == "MAP":
            nid = msg.get("id")
            if nid is None: return
            x = float(msg.get("x", 0))
            y = float(msg.get("y", 0))
            z = float(msg.get("z", 0))

            # Store anchor position
            self.anchor_map[nid] = {"x":x,"y":y,"z":z}

            # Forward to laptop
            emit({
                "type": "MAP", "id": nid,
                "x": round(x,4), "y": round(y,4), "z": round(z,4),
                "rssi": self.comms.rssi(),
                "snr":  round(self.comms.snr(), 2),
            })
            print("[Rover] Heard node {} at ({:.3f},{:.3f})".format(nid,x,y))

        elif t == "OFFLINE":
            nid = msg.get("id")
            if nid:
                self.anchor_map.pop(nid, None)
                emit({"type":"OFFLINE","id":nid})

        elif t == "HEARTBEAT":
            # Respond so fixed nodes know rover is present
            self.comms.send({"type":"PONG","id":self.rover_id,
                             "x": round(self.coords[0],4) if self.coords else 0.0,
                             "y": round(self.coords[1],4) if self.coords else 0.0,
                             "z": round(self.coords[2],4) if self.coords else 0.0})

    def _self_localise(self):
        """
        Rover takes UWB readings against visible fixed anchors
        and computes its own position.
        """
        if len(self.anchor_map) < MIN_ANCHORS:
            return

        print("[Rover] Self-localising against {} anchors...".format(
            len(self.anchor_map)))
        self._oled("ROVER\nRanging...")

        try:
            # Flush and scan
            self.uwb.flush()
            raw = self.uwb.scan(10)
            slot_dists = sorted([d for d in raw.values() if d and d > 0])

            if not slot_dists:
                print("[Rover] No UWB distances")
                return

            # Match distances to anchors sorted by expected proximity
            if self.coords:
                cx,cy,cz = self.coords
                def prox(item):
                    p = item[1]
                    return math.sqrt((p["x"]-cx)**2+(p["y"]-cy)**2)
                anchors_sorted = sorted(self.anchor_map.items(), key=prox)
            else:
                anchors_sorted = sorted(self.anchor_map.items())

            measurements = []
            for i, (aid, apos) in enumerate(anchors_sorted):
                if i >= len(slot_dists): break
                measurements.append(((apos["x"],apos["y"],apos["z"]),
                                     slot_dists[i]))
                print("[Rover]   anchor {} d={:.3f}m".format(aid,slot_dists[i]))

            result = _trilaterate(measurements)
            if result is None:
                return

            x, y, z = result
            self.coords = (x, y, z)
            print("[Rover] Position: ({:.3f},{:.3f},{:.3f})".format(x,y,z))

            # Emit rover position to laptop
            emit({
                "type": "MAP",
                "id":   self.rover_id,
                "x":    round(x,4),
                "y":    round(y,4),
                "z":    round(z,4),
                "rssi": 0,
                "snr":  0.0,
            })

        except Exception as e:
            print("[Rover] Localise error:", e)
        finally:
            self._oled("ROVER\nListening...")

    def _refresh_oled(self):
        anchors = len(self.anchor_map)
        if self.coords:
            x,y,z = self.coords
            text = "ROVER\n{:.2f},{:.2f}\nAnch:{}".format(x,y,anchors)
        else:
            text = "ROVER\nListening\nAnch:{}".format(anchors)
        self._oled(text)

    def _oled(self, text):
        if not self.oled: return
        try: self.oled.display_text(text)
        except: pass


def main():
    cfg = {"id": 99, "name": "rover"}
    try:
        import ujson
        with open("config.json") as f:
            cfg.update(ujson.load(f))
    except Exception as e:
        print("[CONFIG]", e)

    rover_id = int(cfg.get("id", 99))
    print("[CONFIG] Rover id={}".format(rover_id))

    oled  = init_oled()
    comms = Comms()
    uwb   = BU03()

    Rover(rover_id, comms, uwb, oled).run()


main()

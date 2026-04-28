#!/usr/bin/env python3
"""
server.py — UWB Localisation Gateway Server
=============================================
Reads JSON lines from Node 0 over USB serial.
Handles MAP, DIST, and OFFLINE messages.
Runs trilateration on DIST messages (laptop-side).
Logs all events to CSV via logger.py.
Serves live map via WebSocket + HTTP.

Requirements:
    pip install pyserial websockets

Usage:
    python server.py                              # auto-detect port
    python server.py --port COM3                  # Windows
    python server.py --port /dev/ttyACM0          # Linux
    python server.py --scenario scenario1         # sets log filename
"""

import argparse, asyncio, json, sys, time, threading, os
import serial, serial.tools.list_ports
import websockets
from http.server import HTTPServer, SimpleHTTPRequestHandler
from logger  import Logger

WS_HOST   = "localhost"
WS_PORT   = 8765
HTTP_PORT = 8080
BAUD      = 115200
STALE_S   = 120

# ── Shared state ──────────────────────────────────────────────────────────────
node_map  = {}        # id → {id,x,y,z,rssi,snr,last_seen}
_clients  = set()
_map_lock = threading.Lock()
_log      = None      # Logger instance, set in main()

# ── Serial ────────────────────────────────────────────────────────────────────

def auto_port():
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if any(k in p.description.upper()
               for k in ("USB","ACM","SILABS","CH340","FTDI","CP210")):
            return p.device
    return ports[0].device if ports else None


def serial_thread(port, loop, bcast, bcast_stale):
    print("[Serial] Connecting to {} @ {}...".format(port, BAUD))
    while True:
        try:
            with serial.Serial(port, BAUD, timeout=1) as ser:
                print("[Serial] Connected to {}".format(port))
                while True:
                    raw = ser.readline()
                    if not raw: continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line: continue

                    # Only parse lines that are JSON objects
                    if not line.startswith("{"):
                        print("[Node0]", line)
                        continue

                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        print("[Node0]", line)
                        continue

                    t = msg.get("type")

                    if t == "MAP":
                        _handle_map(msg, loop, bcast)

                    elif t == "DIST":
                        # DIST is handled on the node — only MAP updates the map
                        pass

                    elif t == "OFFLINE":
                        _handle_offline(msg, loop, bcast_stale)

        except serial.SerialException as e:
            print("[Serial] Error:", e, "— retrying in 3s")
            time.sleep(3)


def _handle_map(msg, loop, bcast):
    nid = msg.get("id")
    if nid is None: return

    prev = node_map.get(nid)
    entry = {
        "id":        nid,
        "x":         float(msg.get("x", 0)),
        "y":         float(msg.get("y", 0)),
        "z":         float(msg.get("z", 0)),
        "rssi":      int(msg.get("rssi", 0)),
        "snr":       float(msg.get("snr", 0)),
        "last_seen": time.time(),
    }
    with _map_lock:
        node_map[nid] = entry

    print("[Map] Node {}: ({:.3f}, {:.3f}, {:.3f})".format(
        nid, entry["x"], entry["y"], entry["z"]))

    if _log:
        if prev is None:
            _log.node_localised(nid, entry["x"], entry["y"], entry["z"])
        else:
            _log.node_position_updated(nid, entry["x"], entry["y"], entry["z"],
                                       prev["x"], prev["y"], prev["z"])

    asyncio.run_coroutine_threadsafe(bcast(entry), loop)




def _handle_offline(msg, loop, bcast_stale):
    nid = msg.get("id")
    if nid is None: return
    with _map_lock:
        node_map.pop(nid, None)
    print("[Map] Node {} went offline".format(nid))
    if _log:
        _log.node_offline(nid)
    asyncio.run_coroutine_threadsafe(bcast_stale([nid]), loop)

# ── WebSocket ─────────────────────────────────────────────────────────────────

async def ws_handler(ws):
    _clients.add(ws)
    try:
        with _map_lock:
            snap = list(node_map.values())
        await ws.send(json.dumps({"type":"SNAPSHOT","nodes":snap}))
        async for _ in ws:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(ws)


async def broadcast(entry):
    if not _clients: return
    msg = json.dumps({"type":"MAP", **entry})
    await asyncio.gather(*[c.send(msg) for c in list(_clients)],
                         return_exceptions=True)


async def broadcast_stale(ids):
    if not _clients: return
    msg = json.dumps({"type":"STALE","ids":ids})
    await asyncio.gather(*[c.send(msg) for c in list(_clients)],
                         return_exceptions=True)


async def stale_cleaner():
    while True:
        await asyncio.sleep(30)
        cutoff = time.time() - STALE_S
        with _map_lock:
            stale = [nid for nid,e in node_map.items() if e["last_seen"] < cutoff]
            for nid in stale:
                del node_map[nid]
                if _log: _log.node_offline(nid)
                print("[Map] Node {} stale — removed".format(nid))
        if stale:
            await broadcast_stale(stale)

# ── HTTP ──────────────────────────────────────────────────────────────────────

def http_thread():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    httpd = HTTPServer(("", HTTP_PORT), SimpleHTTPRequestHandler)
    print("[HTTP] Serving at http://localhost:{}/map.html".format(HTTP_PORT))
    httpd.serve_forever()

# ── Main ──────────────────────────────────────────────────────────────────────

async def async_main(port, scenario):
    global _log
    _log = Logger(scenario)

    loop = asyncio.get_event_loop()
    ws_server = await websockets.serve(ws_handler, WS_HOST, WS_PORT)
    print("[WS] WebSocket on ws://{}:{}".format(WS_HOST, WS_PORT))

    threading.Thread(target=http_thread, daemon=True).start()
    threading.Thread(
        target=serial_thread,
        args=(port, loop, broadcast, broadcast_stale),
        daemon=True).start()

    print("\n>>> Open http://localhost:{}/map.html\n".format(HTTP_PORT))
    await asyncio.gather(stale_cleaner(), ws_server.wait_closed())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",     default=None)
    ap.add_argument("--scenario", default="run",
                    help="Scenario name for log file (default: run)")
    args = ap.parse_args()

    port = args.port or auto_port()
    if not port:
        print("[ERROR] No serial port found. Use --port.")
        sys.exit(1)
    print("[Serial] Using port:", port)

    try:
        asyncio.run(async_main(port, args.scenario))
    except KeyboardInterrupt:
        if _log: _log.close()
        print("\n[Server] Stopped")


if __name__ == "__main__":
    main()

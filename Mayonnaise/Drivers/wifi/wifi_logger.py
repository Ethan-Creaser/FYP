import gc
import network
import uselect
import utime
import usocket


_BUF_MAX = 200

_HTML = b"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Egg Monitor</title><style>
body{background:#111;color:#0f0;font:13px/1.5 monospace;margin:0}
#hdr{position:fixed;top:0;left:0;right:0;background:#1a1a1a;padding:4px 10px;
     font-size:11px;color:#888;border-bottom:1px solid #333;z-index:9}
#log{padding:8px 10px;margin-top:26px;white-space:pre-wrap;word-break:break-all}
</style></head>
<body>
<div id="hdr">Egg Monitor &mdash; <span id="st">connecting&hellip;</span>
  <a href="javascript:void(0)" onclick="es.close();document.getElementById('st').textContent='paused'"
     style="float:right;color:#555;text-decoration:none">pause</a>
</div>
<div id="log"></div>
<script>
var log=document.getElementById('log'),st=document.getElementById('st');
var es=new EventSource('/events');
es.onopen=function(){st.textContent='live'};
es.onerror=function(){st.textContent='reconnecting…'};
es.onmessage=function(e){
  log.textContent+=e.data+'\\n';
  window.scrollTo(0,document.body.scrollHeight);
};
</script>
</body></html>
"""

_SSE_HEADERS = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Cache-Control: no-cache\r\n"
    b"Connection: keep-alive\r\n"
    b"Access-Control-Allow-Origin: *\r\n\r\n"
)

_STA_CONNECT_TIMEOUT_MS = 15000


class WiFiLogger:
    """
    Connects the egg to an existing WiFi network (STA mode, default) or creates
    its own access point (AP mode), then serves a browser log monitor over HTTP/SSE.

    Config keys:
        wifi_mode     "sta" (default) or "ap"
        wifi_ssid     Network to join (STA) or AP name (AP)
        wifi_password Network / AP password
        wifi_port     HTTP port (default 80)

    Call poll() each main-loop iteration to accept browser connections.
    Call log(line) to push a line to all connected browsers and the buffer.
    """

    def __init__(self, ssid, password="", port=80, mode="sta"):
        self._buf = []
        self._clients = []
        self.ip = None

        if mode == "sta":
            self.ip = self._start_sta(ssid, password)
        else:
            self.ip = self._start_ap(ssid, password)

        if self.ip is None:
            raise RuntimeError("WiFi connection failed")

        self._srv = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        try:
            self._srv.setsockopt(usocket.SOL_SOCKET, usocket.SO_REUSEADDR, 1)
        except Exception:
            pass
        self._srv.bind(("0.0.0.0", port))
        self._srv.listen(4)

        self._poll = uselect.poll()
        self._poll.register(self._srv, uselect.POLLIN)

        print("WiFi logger ready: http://{}:{}".format(self.ip, port))

    def _start_sta(self, ssid, password):
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        if sta.isconnected():
            sta.disconnect()
            utime.sleep_ms(200)
        sta.connect(ssid, password)
        start = utime.ticks_ms()
        while not sta.isconnected():
            if utime.ticks_diff(utime.ticks_ms(), start) > _STA_CONNECT_TIMEOUT_MS:
                print("WiFi STA: failed to connect to '{}'".format(ssid))
                return None
            utime.sleep_ms(200)
        ip = sta.ifconfig()[0]
        print("WiFi STA: connected to '{}' ip={}".format(ssid, ip))
        return ip

    def _start_ap(self, ssid, password):
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
        ap.config(essid=ssid, password=password, authmode=3 if password else 0)
        ap.active(True)
        start = utime.ticks_ms()
        while not ap.active():
            if utime.ticks_diff(utime.ticks_ms(), start) > 5000:
                print("WiFi AP: failed to start")
                return None
            utime.sleep_ms(100)
        ip = ap.ifconfig()[0]
        print("WiFi AP: ssid='{}' ip={}".format(ssid, ip))
        return ip

    def log(self, line):
        self._buf.append(line)
        if len(self._buf) > _BUF_MAX:
            self._buf.pop(0)

        dead = []
        for cl in self._clients:
            try:
                cl.write("data: {}\n\n".format(line))
            except Exception:
                dead.append(cl)
        for cl in dead:
            self._clients.remove(cl)
            try:
                cl.close()
            except Exception:
                pass

    def poll(self):
        if self._poll.poll(0):
            try:
                conn, _ = self._srv.accept()
                self._handle(conn)
            except Exception:
                pass
        gc.collect()

    def _handle(self, conn):
        conn.settimeout(1.0)
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(256)
                if not chunk:
                    break
                req += chunk
        except Exception:
            conn.close()
            return

        req_str = req.decode("utf-8", "ignore")

        if "GET /events" in req_str:
            try:
                conn.send(_SSE_HEADERS)
                for line in self._buf:
                    conn.write("data: {}\n\n".format(line))
            except Exception:
                conn.close()
                return
            conn.setblocking(False)
            self._clients.append(conn)

        elif "GET /" in req_str:
            try:
                conn.send(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                    "Content-Length: {}\r\nConnection: close\r\n\r\n".format(
                        len(_HTML)
                    ).encode()
                )
                conn.write(_HTML)
            except Exception:
                pass
            conn.close()

        else:
            try:
                conn.send(b"HTTP/1.1 404 Not Found\r\n\r\n")
            except Exception:
                pass
            conn.close()

"""MicroPython-friendly CSV logger for per-packet events (debug folder copy).

This copy is for debug use only; production code should not import from here.
"""
try:
    import utime as time
except Exception:
    import time

LOG_DIR = "logs"
LOG_FILE = LOG_DIR + "/packets.csv"
HEADER = "timestamp_ms,node_id,event,seq,src,dst,attempts,rssi,snr,rtt_ms,result,note"


def _now_ms():
    try:
        return int(time.time() * 1000)
    except Exception:
        try:
            return int(time.ticks_ms())
        except Exception:
            return 0


def _ensure_header():
    try:
        with open(LOG_FILE, "r"):
            return
    except Exception:
        # try to create logs dir
        try:
            import os
            if LOG_DIR not in os.listdir():
                os.mkdir(LOG_DIR)
        except Exception:
            pass
        try:
            with open(LOG_FILE, "a") as f:
                f.write(HEADER + "\n")
        except Exception:
            pass


def _quote(s):
    if s is None:
        return ""
    s = str(s)
    if any(c in s for c in (",", '"', "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s


def log_event(node_id, event, seq="", src="", dst="", attempts="", rssi="", snr="", rtt_ms="", result="", note=""):
    _ensure_header()
    ts = _now_ms()
    cols = [str(ts), str(node_id), event, str(seq), str(src), str(dst), str(attempts), str(rssi), str(snr), str(rtt_ms), str(result), _quote(note)]
    try:
        with open(LOG_FILE, "a") as f:
            f.write(','.join(cols) + "\n")
    except Exception:
        pass


# Convenience wrappers
def log_send(node_id, seq, dst, attempts=1):
    log_event(node_id, "SEND", seq=seq, dst=dst, attempts=attempts)


def log_ack(node_id, seq, src, rssi=None, snr=None, rtt_ms=None):
    log_event(node_id, "ACK", seq=seq, src=src, rssi=rssi, snr=snr, rtt_ms=rtt_ms, result="DELIVERED")


def log_retry(node_id, seq, attempts):
    log_event(node_id, "RETRY", seq=seq, attempts=attempts)


def log_timeout(node_id, seq):
    log_event(node_id, "TIMEOUT", seq=seq, result="TIMEOUT")


def log_bad_rx(node_id, note, rssi=None, snr=None):
    log_event(node_id, "BAD_RX", note=note, rssi=rssi, snr=snr)

"""Hardware test runner for ESP nodes (moved to Debug/).

Same behaviour as the root-level `hw_runner.py` but relocated for repo tidiness.
"""

try:
    import utime as time
except Exception:
    import time

import sys
if "/" not in sys.path:
    sys.path.insert(0, "/")

import json
import packets
import constants
from node import Node
try:
    import random
except Exception:
    random = None
try:
    import csv_logger
except Exception:
    csv_logger = None


def main():
    cfg_path = "config.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print("hw_runner: failed to load config.json:", e)
        return

    # prefer identity.bin when available
    try:
        from identity import get_ids
        node_id, uwb_id = get_ids(cfg_path=cfg_path)
    except Exception:
        node_id = int(cfg.get("node_id", 1))
        uwb_id = None
    allowed = cfg.get("allowed_neighbors")
    allowlist = set(allowed) if allowed else None

    node = Node(node_id, allowlist=allowlist)

    if not cfg.get("use_hardware"):
        print("hw_runner: use_hardware is false in config.json — set it true to run on device")
        return

    ok = node.attach_hardware_from_config(cfg_path)
    if not ok:
        print("hw_runner: hardware attach failed")
        return

    # prefer background polling if available
    if getattr(node, "radio", None) and hasattr(node.radio, "start_background"):
        try:
            node.radio.start_background(timeout_ms=300)
        except Exception as e:
            print("hw_runner: could not start background poll:", e)

    # attach OLED/status if available
    try:
        from oled_status import OLEDStatus
        display = OLEDStatus()
        try:
            display.attach_node(node)
        except Exception:
            pass
        node.display = display
    except Exception:
        display = None

    # Determine peer target: if hw_test_target set in config use it.
    # If not set and node_id is 1 or 2, default to the other peer.
    tcfg = cfg.get("hw_test_target")
    if tcfg is not None:
        target = int(tcfg)
    else:
        if node_id == 1:
            target = 2
        elif node_id == 2:
            target = 1
        else:
            target = constants.GROUND_STATION_ID

    # periodic loop settings
    interval = float(cfg.get("hw_test_interval_s", 10))
    ack_timeout = float(cfg.get("hw_test_ack_timeout_s", 10))

    hw_test_max_retries = int(cfg.get("hw_test_max_retries", 2))

    # randomized initial offset to avoid synchronized collisions across nodes
    try:
        initial_jitter = random.random() * interval
    except Exception:
        initial_jitter = (node.node_id % 5) * 0.1 * interval
    next_test = time.time() + initial_jitter

    # per-seq test state for retries
    test_state = {}

    print(f"hw_runner: starting periodic test to {target}, interval={interval}s ack_timeout={ack_timeout}s max_retries={hw_test_max_retries}")
    try:
        while True:
            now = time.time()

            # schedule new test packet
            if now >= next_test:
                seq = node.next_seq()
                payload = b"hw_test"
                pkt = packets.make_data(src=node.node_id, dst=target, seq=seq, ttl=constants.MAX_TTL, app_id=constants.APP_CTRL, subtype=1, data=payload)
                node.outstanding[(node.node_id, seq)] = time.time()
                test_state[seq] = {"pkt": pkt, "sent_time": time.time(), "attempts": 1}
                try:
                    node.send_packet(pkt)
                except Exception as e:
                    print("hw_runner: test send error:", e)
                try:
                    if csv_logger:
                        csv_logger.log_send(node.node_id, seq, target, attempts=1)
                except Exception:
                    pass
                try:
                    if display:
                        display.update_on_send(seq, target)
                except Exception:
                    pass
                try:
                    jitter = random.random() * interval
                except Exception:
                    jitter = (node.node_id % 5) * 0.1 * interval
                next_test = now + interval + jitter

            # check outstanding test packets for ACK/timeouts
            for tseq in list(test_state.keys()):
                state = test_state[tseq]
                key = (node.node_id, tseq)
                # ack received
                if key not in node.outstanding:
                    try:
                        if display:
                            display.update_on_ack(tseq)
                    except Exception:
                        pass
                    del test_state[tseq]
                    continue
                # timeout waiting for ack
                if time.time() - state["sent_time"] > ack_timeout:
                    if state["attempts"] <= hw_test_max_retries:
                        state["attempts"] += 1
                        state["sent_time"] = time.time()
                        try:
                            node.send_packet(state["pkt"])
                        except Exception as e:
                            print("hw_runner: test retry send error:", e)
                        try:
                            if csv_logger:
                                csv_logger.log_retry(node.node_id, tseq, state["attempts"])
                        except Exception:
                            pass
                        print(f"hw_runner: retry seq={tseq} attempt={state['attempts']}")
                    else:
                        print(f"hw_runner: timeout seq={tseq}")
                        try:
                            if csv_logger:
                                csv_logger.log_timeout(node.node_id, tseq)
                        except Exception:
                            pass
                        try:
                            if display:
                                display.update_on_timeout(tseq)
                        except Exception:
                            pass
                        try:
                            del node.outstanding[key]
                        except Exception:
                            pass
                        del test_state[tseq]

            # poll radio if needed or sleep briefly
            if getattr(node, "radio", None) and not getattr(node.radio, "_bg_running", False):
                try:
                    node.radio.poll(timeout_ms=300)
                except Exception as e:
                    print("hw_runner: radio poll error:", e)
                    try:
                        time.sleep(0.2)
                    except Exception:
                        pass
            else:
                try:
                    time.sleep(0.2)
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("hw_runner: interrupted, stopping")


if __name__ == "__main__":
    main()

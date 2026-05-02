"""Hardware test runner for ESP nodes.

Behaviour:
- load `config.json`
- attach hardware radio
- send a single test DATA packet to `hw_test_target` (defaults to ground station)
- wait for hop-by-hop ACK (timeout)

Flash this to each ESP and run to verify basic send/ACK behavior.
"""

try:
    import utime as time
except Exception:
    import time

import json
import packets
import constants
from node import Node


def main():
    cfg_path = "config.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print("hw_runner: failed to load config.json:", e)
        return

    node_id = int(cfg.get("node_id", 1))
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

    target = int(cfg.get("hw_test_target", constants.GROUND_STATION_ID))

    # craft and send packet using node.next_seq() so we know the seq
    seq = node.next_seq()
    payload = b"hw_test"
    pkt = packets.make_data(src=node.node_id, dst=target, seq=seq, ttl=constants.MAX_TTL, app_id=constants.APP_CTRL, subtype=1, data=payload)

    # mark outstanding so we can detect ACK
    node.outstanding[(node.node_id, seq)] = time.time()

    print(f"hw_runner: sending test packet seq={seq} -> {target}")
    node.send_packet(pkt)

    # wait for ACK up to timeout
    timeout_s = 10
    start = time.time()
    key = (node.node_id, seq)
    while key in node.outstanding and (time.time() - start) < timeout_s:
        # if radio has poll and no background thread, poll here
        if getattr(node, "radio", None) and not getattr(node.radio, "_bg_running", False):
            try:
                node.radio.poll(timeout_ms=300)
            except Exception as e:
                print("hw_runner: radio poll error:", e)
                break
        else:
            # small sleep while waiting for background thread
            try:
                time.sleep(0.2)
            except Exception:
                pass

    if key not in node.outstanding:
        print(f"hw_runner: ACK received for seq={seq}")
    else:
        print(f"hw_runner: timeout waiting for ACK for seq={seq}")


if __name__ == "__main__":
    main()

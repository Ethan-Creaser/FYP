#!/usr/bin/env python3
"""Rewrite identity.bin (node_id, uwb_id, allowed_neighbors) on live eggs over BLE.

No reflash needed — changes take effect immediately; the allowlist is also
updated in RAM so beaconing behaviour changes within one beacon interval.

Topology file format (JSON):
    {
        "egg_6": {"uwb_id": 6, "neighbors": [7, 10]},
        "egg_7": {"uwb_id": 7, "neighbors": [6, 8]},
        "egg_8": {"uwb_id": 8, "neighbors": [7, 9]}
    }

    Key  : BLE advertisement name (e.g. "egg_6") — node_id is parsed from it.
    uwb_id    : UWB slot ID (0-7); 0 = tag role, 1-7 = anchor role.
    neighbors : list of node_ids this egg is allowed to hear from (topology edges).
                Use [] to remove the restriction (accept any neighbour).

Modes
-----
Check only (no BLE) — validate that all neighbor links are bidirectional:
    python bt_topology.py topology.json --check

Direct (default) — connect to every egg in the file one by one:
    python bt_topology.py topology.json

Via-gateway — connect to one gateway egg; it relays commands over LoRa mesh:
    python bt_topology.py topology.json --via egg_6

Single egg (inline, no file):
    python bt_topology.py --egg egg_7 --uwb-id 7 --neighbors 6 8

The symmetry check always runs when loading a file.  Asymmetric edges are
printed as warnings but do NOT abort the send — fix the file and rerun.

Requirements:
    pip install bleak
"""

import argparse
import asyncio
import sys

from topology import Topology

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("bleak is required:  pip install bleak")
    sys.exit(1)

NUS_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"   # PC → egg (write)
NUS_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"   # egg → PC (notify)

BT_CMD_IDENTITY = 0xD1   # must match _BT_CMD_IDENTITY in main.py
SCAN_TIMEOUT    = 8.0
ACK_WAIT        = 4.0    # seconds to listen for the egg's confirmation print


def _node_id_from_name(name):
    """Parse node_id from 'egg_<N>' → N.  Raises ValueError if not parseable."""
    try:
        return int(str(name).lower().removeprefix("egg_"))
    except ValueError:
        raise ValueError("Cannot parse node_id from BLE name '{}'".format(name))


def _build_payload(target_id, uwb_id, neighbors):
    """Build the 0xD1 binary command: [cmd, target_id, uwb_id, count, n0, n1, ...]"""
    nb = [int(n) & 0xFF for n in neighbors]
    if len(nb) > 255:
        raise ValueError("Too many neighbors (max 255)")
    return bytes([BT_CMD_IDENTITY, target_id & 0xFF, uwb_id & 0xFF, len(nb)] + nb)


# ---------------------------------------------------------------------------
# BLE helpers
# ---------------------------------------------------------------------------

async def _find(name):
    print("  Scanning for '{}'…".format(name))
    device = await BleakScanner.find_device_by_name(name, timeout=SCAN_TIMEOUT)
    if device is None:
        print("  ERROR: '{}' not found — is it powered and advertising?".format(name))
    return device


async def _send_and_listen(client, payload, label):
    """Write payload to RX char; print any egg response for ACK_WAIT seconds."""
    buf = {"text": ""}

    def on_notify(_handle, data):
        try:
            buf["text"] += data.decode("utf-8", errors="replace")
        except Exception:
            pass
        while "\n" in buf["text"]:
            line, buf["text"] = buf["text"].split("\n", 1)
            line = line.rstrip()
            if line:
                print("  [{}] {}".format(label, line))

    await client.start_notify(NUS_TX_UUID, on_notify)
    await client.write_gatt_char(NUS_RX_UUID, payload, response=True)
    await asyncio.sleep(ACK_WAIT)
    await client.stop_notify(NUS_TX_UUID)


# ---------------------------------------------------------------------------
# Direct mode: connect to each egg individually
# ---------------------------------------------------------------------------

async def write_direct(entries):
    """entries: list of (ble_name, node_id, uwb_id, neighbors)"""
    ok_count = 0
    for ble_name, node_id, uwb_id, neighbors in entries:
        print("\n[{}] node_id={} uwb_id={} neighbors={}".format(
            ble_name, node_id, uwb_id, neighbors))
        device = await _find(ble_name)
        if device is None:
            continue
        payload = _build_payload(node_id, uwb_id, neighbors)
        try:
            async with BleakClient(device) as client:
                print("  Connected ({})".format(device.address))
                await _send_and_listen(client, payload, ble_name)
            print("  Done.")
            ok_count += 1
        except Exception as exc:
            print("  ERROR: {}".format(exc))
    return ok_count


# ---------------------------------------------------------------------------
# Gateway mode: one BLE connection, relay everything via LoRa mesh
# ---------------------------------------------------------------------------

async def write_via_gateway(via_name, entries):
    """entries: list of (ble_name, node_id, uwb_id, neighbors)"""
    device = await _find(via_name)
    if device is None:
        return 0

    ok_count = 0
    try:
        async with BleakClient(device) as client:
            print("  Connected to gateway {} ({})".format(via_name, device.address))
            for ble_name, node_id, uwb_id, neighbors in entries:
                print("\n  → egg_{} uwb_id={} neighbors={}".format(
                    node_id, uwb_id, neighbors))
                payload = _build_payload(node_id, uwb_id, neighbors)
                await _send_and_listen(client, payload, via_name)
                ok_count += 1
    except Exception as exc:
        print("ERROR: {}".format(exc))
    return ok_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_topology(path):
    try:
        topo = Topology.load(path)
    except Exception as e:
        print("Cannot read topology file '{}': {}".format(path, e))
        sys.exit(1)

    issues = topo.validate()
    if not issues:
        print("Topology check: OK — all neighbour relationships are symmetric.")
    else:
        print("Topology check: ASYMMETRIC EDGES DETECTED")
        print("-" * 48)
        for issue in issues:
            print(" ", issue)
        print("-" * 48)

    return list(topo.entries())


def main():
    parser = argparse.ArgumentParser(
        description="Rewrite egg identity.bin (topology) over BLE without reflashing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Write topology.json to all eggs directly:
  python bt_topology.py topology.json

  # Write via a single gateway (relayed over LoRa mesh):
  python bt_topology.py topology.json --via egg_6

  # Single egg, no file:
  python bt_topology.py --egg egg_7 --uwb-id 7 --neighbors 6 8
        """,
    )
    parser.add_argument("file", nargs="?", help="Topology JSON file")
    parser.add_argument("--via", metavar="NAME",
                        help="Gateway egg BLE name; relay commands over LoRa mesh")
    parser.add_argument("--egg", metavar="NAME",
                        help="Single egg BLE name (inline mode, no file)")
    parser.add_argument("--uwb-id", type=int, metavar="N",
                        help="UWB slot ID for --egg (default = node_id)")
    parser.add_argument("--neighbors", type=int, nargs="*", default=[],
                        metavar="N",
                        help="Neighbor node_ids for --egg (default = [] = no restriction)")
    parser.add_argument("--check", action="store_true",
                        help="Validate topology symmetry and exit without connecting")
    args = parser.parse_args()

    if args.check:
        if not args.file:
            print("--check requires a topology file")
            sys.exit(1)
        try:
            topo = Topology.load(args.file)
        except Exception as e:
            print("Cannot read '{}': {}".format(args.file, e))
            sys.exit(1)
        issues = topo.validate()
        if not issues:
            print("Topology check: OK — all neighbour relationships are symmetric.")
            sys.exit(0)
        print("Topology check: ASYMMETRIC EDGES DETECTED")
        print("-" * 48)
        for issue in issues:
            print(" ", issue)
        print("-" * 48)
        sys.exit(1)

    if args.egg:
        # inline single-egg mode
        try:
            node_id = _node_id_from_name(args.egg)
        except ValueError as e:
            print("ERROR:", e)
            sys.exit(1)
        uwb_id = args.uwb_id if args.uwb_id is not None else node_id
        entries = [(args.egg, node_id, uwb_id, args.neighbors or [])]
    elif args.file:
        entries = _load_topology(args.file)
    else:
        parser.print_help()
        sys.exit(1)

    if not entries:
        print("No valid entries — nothing to do.")
        sys.exit(0)

    print("=== Topology writer ===")
    print("Mode    : {}".format("gateway ({})".format(args.via) if args.via else "direct"))
    print("Entries : {}".format(len(entries)))

    if args.via:
        ok = asyncio.run(write_via_gateway(args.via, entries))
    else:
        ok = asyncio.run(write_direct(entries))

    print("\n{}/{} egg(s) updated.".format(ok, len(entries)))
    sys.exit(0 if ok == len(entries) else 1)


if __name__ == "__main__":
    main()

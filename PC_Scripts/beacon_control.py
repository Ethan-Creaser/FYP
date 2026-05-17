"""Beacon Control — enable or disable beaconing on eggs remotely.

Connects to a gateway egg over BLE and sends a 0xD2 command.  The gateway
either applies the change to itself (if it is the target) or forwards it
through the mesh to the target egg.  The change is written to identity.bin
on the target so it persists across reboots.

Requires: pip install bleak

Usage:
    python beacon_control.py --name egg_6 --target 7 --disable
    python beacon_control.py --name egg_6 --target 7 --enable
    python beacon_control.py --name egg_6 --target 6 7 8 9 --disable
"""

import argparse
import asyncio
import sys
import time as _time

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("bleak not installed — run: pip install bleak")
    sys.exit(1)

NUS_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

CMD_BEACON   = 0xD2
SCAN_TIMEOUT = 10.0
ACK_TIMEOUT  = 20.0   # seconds to wait for BEACON_OK per target


async def run(via_name, target_ids, enable):
    action = "ENABLE" if enable else "DISABLE"
    print("\nScanning for '{}'…".format(via_name))
    device = await BleakScanner.find_device_by_name(via_name, timeout=SCAN_TIMEOUT)
    if device is None:
        print("ERROR: '{}' not found.".format(via_name))
        return False

    print("Found {} ({})".format(device.name, device.address))

    confirmed = set()
    buf       = ""

    def on_notify(_handle, data):
        nonlocal buf
        buf += data.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip()
            if line:
                print("[egg]", line)
            # BEACON_OK node_id=7 enabled=0
            if line.startswith("BEACON_OK"):
                try:
                    parts = {k: v for k, v in (p.split("=") for p in line.split()[1:])}
                    confirmed.add(int(parts["node_id"]))
                except Exception:
                    pass

    async with BleakClient(device) as client:
        if not client.is_connected:
            print("ERROR: failed to connect")
            return False

        print("Connected.\n")
        await client.start_notify(NUS_TX_UUID, on_notify)

        for target_id in target_ids:
            confirmed.discard(target_id)
            payload = bytes([CMD_BEACON, target_id & 0xFF, 1 if enable else 0])
            print("── {} beacon on egg_{} ──".format(action, target_id))
            await client.write_gatt_char(NUS_RX_UUID, payload, response=True)

            deadline = _time.monotonic() + ACK_TIMEOUT
            while _time.monotonic() < deadline:
                await asyncio.sleep(0.3)
                if target_id in confirmed:
                    print("  ✓ egg_{} beacon {}D".format(target_id, action.lower()[:-1]))
                    break
            else:
                print("  ✗ egg_{} no confirmation (timed out after {}s)".format(
                    target_id, ACK_TIMEOUT))

        await client.stop_notify(NUS_TX_UUID)

    print("\nDone.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Enable or disable beaconing on eggs via BLE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python beacon_control.py --name egg_6 --target 7 --disable
  python beacon_control.py --name egg_6 --target 6 7 8 --enable
        """,
    )
    parser.add_argument("--name",   required=True,
                        help="BLE name of gateway egg (e.g. egg_6)")
    parser.add_argument("--target", required=True, nargs="+", type=int, metavar="ID",
                        help="Target egg node ID(s)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable",  action="store_true", help="Enable beaconing")
    group.add_argument("--disable", action="store_true", help="Disable beaconing")
    args = parser.parse_args()

    enable = args.enable
    action = "ENABLE" if enable else "DISABLE"

    print("=== Beacon Control ===")
    print("Gateway : {}".format(args.name))
    print("Targets : {}".format(args.target))
    print("Action  : {}".format(action))
    print()

    asyncio.run(run(args.name, args.target, enable))


if __name__ == "__main__":
    main()

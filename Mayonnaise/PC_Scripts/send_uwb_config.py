"""Send a UWB ID and role to a target egg via a gateway egg over BLE + LoRa mesh.

Usage:
    python send_uwb_config.py --name egg_6

Flow:
    PC --[BLE NUS write]--> gateway egg --[LoRa mesh]--> target egg
    The target egg reconfigures its UWB module at runtime (identity.bin is not changed).

Requires:
    pip install bleak
"""

import argparse
import asyncio
import sys

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("bleak not installed — run: pip install bleak")
    sys.exit(1)

NUS_RX_UUID  = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # PC → egg
NUS_TX_UUID  = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # egg → PC

CMD_UWB      = 0xCF   # must match _BT_CMD_UWB in main.py
SCAN_TIMEOUT = 10.0

_notify_buf = ""


def _on_notify(sender, data):
    """Reassemble 20-byte BLE chunks into complete lines before printing."""
    global _notify_buf
    try:
        _notify_buf += data.decode("utf-8")
    except Exception:
        return
    while "\n" in _notify_buf:
        line, _notify_buf = _notify_buf.split("\n", 1)
        line = line.rstrip()
        if line:
            print("[egg]", line)


def prompt_int(prompt, lo, hi):
    while True:
        raw = input(prompt).strip()
        try:
            val = int(raw)
        except ValueError:
            print("  Please enter a whole number.")
            continue
        if lo <= val <= hi:
            return val
        print("  Must be between {} and {}.".format(lo, hi))


async def send_uwb_config(via_name, target_egg_id, uwb_id, role):
    print("\nScanning for '{}'...".format(via_name))
    device = await BleakScanner.find_device_by_name(via_name, timeout=SCAN_TIMEOUT)
    if device is None:
        print("ERROR: '{}' not found. Is the egg powered and advertising?".format(via_name))
        return False

    print("Found {} ({})".format(device.name, device.address))

    async with BleakClient(device) as client:
        if not client.is_connected:
            print("ERROR: failed to connect")
            return False

        print("Connected. Listening for egg output...")
        await client.start_notify(NUS_TX_UUID, _on_notify)

        # [CMD_UWB, target_egg_id, uwb_id, role]
        payload = bytes([CMD_UWB, target_egg_id & 0xFF, uwb_id & 0xFF, role & 0xFF])
        await client.write_gatt_char(NUS_RX_UUID, payload, response=True)
        print("Command sent — {} forwarding uwb_id={} role={} to egg_{}".format(
            via_name, uwb_id, role, target_egg_id))

        # wait for the mesh to deliver and the target to confirm
        await asyncio.sleep(3.0)
        await client.stop_notify(NUS_TX_UUID)

    print("\nDone.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Reconfigure a target egg's UWB via the LoRa mesh.")
    parser.add_argument("--name", required=True,
                        help="BLE name of the gateway egg (e.g. egg_6)")
    args = parser.parse_args()

    print("=== UWB Config Tool ===")
    print("Gateway : {}".format(args.name))
    print()

    target_egg_id = prompt_int("Target egg ID  (1-99)         : ", 1, 99)
    uwb_id        = prompt_int("UWB ID         (0-7)          : ", 0, 7)
    role          = prompt_int("UWB role       (0=tag 1=anchor): ", 0, 1)

    expected_role = 0 if uwb_id == 0 else 1
    if role != expected_role:
        print("WARNING: uwb_id={} normally implies role={} "
              "(convention: 0=tag, else anchor)".format(uwb_id, expected_role))

    print("\nSummary:")
    print("  Gateway : {}".format(args.name))
    print("  Target  : egg_{}".format(target_egg_id))
    print("  UWB ID  : {}".format(uwb_id))
    print("  Role    : {} ({})".format(role, "tag" if role == 0 else "anchor"))

    confirm = input("\nSend? [y/N] : ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        sys.exit(0)

    ok = asyncio.run(send_uwb_config(args.name, target_egg_id, uwb_id, role))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

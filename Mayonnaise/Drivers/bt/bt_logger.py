import ubluetooth
import utime
from micropython import const

# BLE IRQ event codes
_IRQ_CENTRAL_CONNECT    = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)

# GATT characteristic flags
_FLAG_NOTIFY = const(0x0010)
_FLAG_WRITE  = const(0x0008)

# Nordic UART Service UUIDs (industry standard BLE serial convention)
_NUS_SVC_UUID = ubluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_NUS_TX_UUID  = ubluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")  # egg → PC
_NUS_RX_UUID  = ubluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")  # PC → egg

_NUS_SERVICE = (_NUS_SVC_UUID, (
    (_NUS_TX_UUID, _FLAG_NOTIFY),
    (_NUS_RX_UUID, _FLAG_WRITE),
))

# BLE can only carry 20 bytes per notify by default; most centrals
# negotiate higher MTU but 20 is the safe floor.
_CHUNK = 20


class BtLogger:
    """
    Advertises the egg as a BLE UART device and streams log lines to any
    connected central (e.g. bt_monitor.py on the PC).

    Uses the Nordic UART Service so any NUS-compatible app can also connect.

    Call log(line) to send a line to the connected central.
    poll() is a no-op kept for API symmetry with WiFiLogger — BLE events
    are driven by the ubluetooth IRQ.
    """

    def __init__(self, name="egg"):
        self._name = name
        self._conn = None

        self._ble = ubluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)

        ((self._tx, _rx),) = self._ble.gatts_register_services((_NUS_SERVICE,))
        self._advertise()
        print("BT: advertising as '{}'".format(name))

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._conn = conn_handle
            # Stop advertising while a central is connected
            self._ble.gap_advertise(None)
            print("BT: central connected")
            self.log("--- egg connected ---")

        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            print("BT: central disconnected, re-advertising")
            self._advertise()

    def _advertise(self):
        name_b = self._name.encode()
        # AD structure: Flags + Complete Local Name
        payload = bytearray()
        payload += bytes([2, 0x01, 0x06])                        # Flags: LE General Discoverable
        payload += bytes([1 + len(name_b), 0x09]) + name_b      # Complete Local Name
        self._ble.gap_advertise(100_000, adv_data=bytes(payload))  # 100 ms interval

    def log(self, line):
        if self._conn is None:
            return
        data = (line + "\n").encode("utf-8")
        for i in range(0, len(data), _CHUNK):
            try:
                self._ble.gatts_notify(self._conn, self._tx, data[i:i + _CHUNK])
                # Small gap so the BLE stack doesn't drop notifications
                utime.sleep_ms(5)
            except Exception:
                self._conn = None
                break

    def poll(self):
        pass  # BLE events are IRQ-driven; kept for API symmetry

import gc
import ubluetooth
import utime
from micropython import const

# BLE IRQ event codes
_IRQ_CENTRAL_CONNECT    = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE        = const(3)

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

_CHUNK = 20


class BtLogger:
    """BLE UART logger using Nordic UART Service.

    Streams all print() output to a connected central (e.g. send_uwb_config.py).
    Set on_rx to a callable(bytes) to handle writes from the central.
    poll() must be called regularly from the main loop to process received data
    safely outside the BLE IRQ context.
    """

    def __init__(self, name="egg"):
        self._name = name
        self._conn = None
        self.on_rx = None        # callable(bytes) — set by main.py
        self._rx_pending = None  # data buffered from IRQ, processed in poll()

        gc.collect()
        utime.sleep_ms(200)
        self._ble = ubluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)

        ((self._tx, self._rx),) = self._ble.gatts_register_services((_NUS_SERVICE,))
        self._advertise()

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._conn = conn_handle
            self._ble.gap_advertise(None)
            print("BT: central connected")
            self.log("--- egg connected ---")

        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            print("BT: central disconnected, re-advertising")
            self._advertise()

        elif event == _IRQ_GATTS_WRITE:
            _, value_handle = data
            if value_handle == self._rx:
                # Buffer only — do NOT call on_rx here. Calling gatts_notify
                # (via the print tee) from inside a BLE IRQ deadlocks the stack.
                self._rx_pending = bytes(self._ble.gatts_read(self._rx))

    def _advertise(self):
        name_b = self._name.encode()
        payload = bytearray()
        payload += bytes([2, 0x01, 0x06])
        payload += bytes([1 + len(name_b), 0x09]) + name_b
        self._ble.gap_advertise(100_000, adv_data=bytes(payload))

    def log(self, line):
        if self._conn is None:
            return
        data = (line + "\n").encode("utf-8")
        for i in range(0, len(data), _CHUNK):
            try:
                self._ble.gatts_notify(self._conn, self._tx, data[i:i + _CHUNK])
                utime.sleep_ms(5)
            except Exception:
                self._conn = None
                break

    def poll(self):
        """Call from the main loop to safely process buffered RX data."""
        if self._rx_pending is not None and self.on_rx:
            data = self._rx_pending
            self._rx_pending = None
            try:
                self.on_rx(data)
            except Exception as e:
                print("BT: on_rx error:", e)

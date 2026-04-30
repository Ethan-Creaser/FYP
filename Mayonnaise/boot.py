# boot.py -- runs before main.py
# BLE must be activated here, before any SPI/I2C/UART drivers start,
# otherwise the BLE stack fails to claim its DMA resources on some eggs.
import sys
sys.path.insert(0, '/core')
sys.path.insert(0, '/image')

try:
    import ubluetooth, utime
    _ble = ubluetooth.BLE()
    utime.sleep_ms(500)
    _ble.active(True)
    del _ble
except Exception as e:
    print("boot: BLE pre-init failed ({})".format(e))

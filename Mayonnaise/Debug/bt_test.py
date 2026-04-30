import ubluetooth, gc, utime
print("free:", gc.mem_free())
utime.sleep_ms(500)
print("creating BLE...")
ble = ubluetooth.BLE()
print("activating...")
ble.active(True)
print("BLE OK - it works on this chip")
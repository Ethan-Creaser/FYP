from machine import I2C, Pin

i2c = I2C(0, scl=Pin(8), sda=Pin(9), freq=400000)

devices = i2c.scan()

if devices:
    print("I2C devices found: {}".format(len(devices)))
    for addr in devices:
        print("  Address: 0x{:02X} ({})".format(addr, addr))
else:
    print("No I2C devices found")

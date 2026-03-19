from machine import SPI, Pin

spi = SPI(2, sck=Pin(36), mosi=Pin(35), miso=Pin(37))
cs = Pin(38, Pin.OUT, value=1)

buf = bytearray(4)
cs.value(0)
spi.readinto(buf)
cs.value(1)
print("Read:", buf)

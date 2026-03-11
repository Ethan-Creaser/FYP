# SSD1315 OLED screen test file
# PINOUT: GND->GND, VCC->3V3, SCL->Pin9, SDA->Pin8
# Install: Thonny > Tools > Manage Packages > micropython-ssd1306

from machine import Pin, I2C
import ssd1306

class OLED:
    def __init__(self, sda=8, scl=9, width=128, height=64, freq=400000):
        i2c = I2C(0, scl=Pin(scl), sda=Pin(sda), freq=freq)
        self.width = width
        self.height = height
        self.display = ssd1306.SSD1306_I2C(width, height, i2c)

    def display_text(self, text, x=0, y=0, clear=True):
        if clear:
            self.display.fill(0)
        self.display.text(text, x, y)
        self.display.show()

    def clear(self):
        self.display.fill(0)
        self.display.show()


# Example usage
oled = OLED()
oled.display_text("Hello World!")

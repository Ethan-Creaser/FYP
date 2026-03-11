# SSD1315 OLED screen test file
# Connects to OLED over I2C and runs test scirpt to check pixels and stuff

# PINOUT
# GND to GND
# VCC to 3V3
# SCL to Pin 9 (or other clk)
# SDA to Pin 8 (or other data)

# TO INSTALL LIBRARY
# Open Thonny, go Tools > Manage Packages, search for micropython-ssd1306 and install onto your ESP



from machine import Pin, I2C
import ssd1306
import time
import math

# 1. Setup I2C based on DevKitC-1 v1.1 Header J1
# SDA is GPIO 8 (Pin 12 on J1), SCL is GPIO 9 (Pin 15 on J1)
i2c = I2C(0, scl=Pin(9), sda=Pin(8), freq=400000)

# 2. Initialize Display
WIDTH = 128
HEIGHT = 64
oled = ssd1306.SSD1306_I2C(WIDTH, HEIGHT, i2c)

def run_test():
    print("Starting OLED Test...")
    
    # Test 1: Text and Border
    oled.fill(0)
    oled.rect(0, 0, 128, 64, 1) # Draw border
    oled.text("ESP32-S3", 30, 10)
    oled.text("DevKitC-1 v1.1", 10, 25)
    oled.text("I2C WORKING!", 15, 45)
    oled.show()
    time.sleep(2)

    # Test 2: Animation (Moving Circle)
    print("Running animation...")
    for i in range(0, 100, 5):
        oled.fill(0)
        oled.text("Scanning...", 10, 10)
        oled.fill_rect(i, 30, 20, 10, 1) # Moving block
        oled.show()
    
    # Test 3: Pixel Density Check
    oled.fill(0)
    for x in range(0, WIDTH, 8):
        for y in range(0, HEIGHT, 8):
            oled.pixel(x, y, 1)
    oled.text("PIXEL TEST", 25, 25)
    oled.show()
    print("Test Complete.")

try:
    run_test()
except Exception as e:
    print(f"Error: {e}")
    print("\nTroubleshooting Checklist:")
    print("1. Is SCL on Pin 9 and SDA on Pin 8 (J1 Header)?")
    print("2. Is the OLED getting 3.3V from Pin 1 or 2?")
    print("3. Did you upload ssd1306.py to the 'lib' folder?")
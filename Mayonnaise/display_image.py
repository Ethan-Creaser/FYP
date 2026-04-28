# display_image.py - MicroPython script to show a pre-converted .bin image on the OLED.
#
# Workflow:
#   1. On your PC, convert the image:
#        python png_to_oled.py mypic.png mypic.bin --dither
#   2. Upload mypic.bin to the ESP32 (e.g. Thonny file upload) alongside this script.
#   3. Upload and run this file.
#
# The .bin must be MONO_VLSB and sized exactly width * (height // 8) bytes
# (1024 bytes for a 128x64 display).

import time
from Drivers.oled.oled_class import OLED


def show_image(oled, path, hold=3):
    """Display a single image and hold it for `hold` seconds."""
    oled.display_image(path)
    time.sleep(hold)


def slideshow(oled, paths, hold=2):
    """Cycle through a list of images."""
    for path in paths:
        try:
            oled.display_image(path)
        except OSError as e:
            # Missing file or wrong size - show the error on screen so you see it
            oled.display_text("Error:\n{}\n{}".format(path, e))
        time.sleep(hold)


if __name__ == "__main__":
    oled = OLED()  # defaults: sda=9, scl=8, 128x64

    # --- Single image ---
    try:
        show_image(oled, "image.bin", hold=10)
    except OSError:
        oled.display_text("No image file.\nUpload image.bin\nto the device.")
        time.sleep(2)

    # --- Slideshow (uncomment to use) ---
    # slideshow(oled, ["logo.bin", "cat.bin", "face.bin"], hold=2)

    # --- Image + text overlay (uncomment to use) ---
    # oled.display_image("bg.bin", clear=True)
    # oled.display.text("Hello!", 0, 0)   # draw text on top of image
    # oled.display.show()

    oled.clear()
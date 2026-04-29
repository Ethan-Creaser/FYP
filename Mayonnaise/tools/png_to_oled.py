#!/usr/bin/env python3
"""
Convert a PNG to a MONO_VLSB binary file for the SSD1306 OLED (128x64).

The output .bin file can be copied to the MicroPython device and displayed
with OLED.display_image().

Usage:
    python tools/png_to_oled.py logo.png logo.bin
    python tools/png_to_oled.py logo.png logo.bin --invert
    python tools/png_to_oled.py logo.png logo.bin --width 64 --height 32 --threshold 100
"""

import argparse
from PIL import Image


def convert(input_path, output_path, width=128, height=64, invert=False, threshold=128):
    img = Image.open(input_path).convert("L")
    img = img.resize((width, height), Image.LANCZOS)

    buf = bytearray(width * (height // 8))
    for y in range(height):
        for x in range(width):
            pixel = img.getpixel((x, y))
            if invert:
                pixel = 255 - pixel
            if pixel >= threshold:
                idx = (y // 8) * width + x
                buf[idx] |= 1 << (y % 8)

    with open(output_path, "wb") as f:
        f.write(buf)
    print("Wrote {} bytes -> {}  ({}x{}, threshold={}{})".format(
        len(buf), output_path, width, height, threshold, ", inverted" if invert else ""
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PNG -> SSD1306 MONO_VLSB .bin converter")
    parser.add_argument("input",           help="Input PNG file")
    parser.add_argument("output", nargs="?", default="image.bin", help="Output .bin file (default: image.bin)")
    parser.add_argument("--width",     type=int, default=128, help="Display width  (default 128)")
    parser.add_argument("--height",    type=int, default=64,  help="Display height (default 64)")
    parser.add_argument("--threshold", type=int, default=128, help="Grey threshold 0-255 for on/off (default 128)")
    parser.add_argument("--invert",    action="store_true",   help="Invert pixel values before thresholding")
    args = parser.parse_args()
    convert(args.input, args.output, args.width, args.height, args.invert, args.threshold)

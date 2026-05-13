#!/usr/bin/env python3
"""
png_to_oled.py - Convert an image to a MONO_VLSB .bin for SSD1306/SSD1315 OLEDs.

Run on your PC (not the ESP32). The output .bin matches the framebuf.MONO_VLSB
format used by MicroPython's framebuf module, so it can be blit directly.

Usage:
    python png_to_oled.py input.png output.bin
    python png_to_oled.py input.jpg output.bin --width 128 --height 64
    python png_to_oled.py photo.png out.bin --invert --dither
    python png_to_oled.py logo.png out.bin --threshold 128 --fit contain

Install dependency:
    pip install Pillow
"""

import argparse
from PIL import Image, ImageOps


def image_to_mono_vlsb(img: Image.Image, width: int, height: int,
                       threshold: int = 128, dither: bool = False,
                       invert: bool = False, fit: str = "contain") -> bytes:
    """
    Convert a PIL image to MONO_VLSB byte layout.

    MONO_VLSB: one byte = 8 vertical pixels, LSB at top.
    Buffer size = width * (height // 8). height must be a multiple of 8.
    """
    if height % 8 != 0:
        raise ValueError("height must be a multiple of 8 for MONO_VLSB")

    # Resize to fit target dimensions
    if fit == "stretch":
        img = img.resize((width, height))
    elif fit == "contain":
        # Preserve aspect ratio, pad with black
        img = ImageOps.contain(img, (width, height))
        canvas = Image.new("L", (width, height), 0)
        ox = (width - img.width) // 2
        oy = (height - img.height) // 2
        canvas.paste(img.convert("L"), (ox, oy))
        img = canvas
    elif fit == "cover":
        # Preserve aspect ratio, crop overflow
        img = ImageOps.fit(img, (width, height))
    else:
        raise ValueError(f"unknown fit mode: {fit}")

    # Convert to 1-bit
    if dither:
        img = img.convert("L").convert("1")  # Floyd-Steinberg by default
    else:
        img = img.convert("L").point(lambda p: 255 if p >= threshold else 0).convert("1")

    if invert:
        img = ImageOps.invert(img.convert("L")).convert("1")

    pixels = img.load()
    pages = height // 8
    buf = bytearray(width * pages)

    # MONO_VLSB packing: buf[page * width + x], bit y%8
    for y in range(height):
        page = y // 8
        bit = y % 8
        for x in range(width):
            # PIL "1" mode: 255 = white, 0 = black. Light pixel -> set bit.
            if pixels[x, y]:
                buf[page * width + x] |= (1 << bit)

    return bytes(buf)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="Input image (png, jpg, bmp, ...)")
    p.add_argument("output", help="Output .bin file")
    p.add_argument("--width", type=int, default=128, help="OLED width (default 128)")
    p.add_argument("--height", type=int, default=64, help="OLED height (default 64)")
    p.add_argument("--threshold", type=int, default=128,
                   help="B/W threshold 0-255 (default 128). Ignored if --dither.")
    p.add_argument("--dither", action="store_true",
                   help="Use Floyd-Steinberg dithering instead of threshold")
    p.add_argument("--invert", action="store_true", help="Invert black/white")
    p.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain",
                   help="How to fit non-matching aspect ratios (default contain)")
    args = p.parse_args()

    img = Image.open(args.input)
    data = image_to_mono_vlsb(img, args.width, args.height,
                              threshold=args.threshold, dither=args.dither,
                              invert=args.invert, fit=args.fit)

    with open(args.output, "wb") as f:
        f.write(data)

    print(f"OK: {args.input} -> {args.output} "
          f"({args.width}x{args.height}, {len(data)} bytes)")


if __name__ == "__main__":
    main()
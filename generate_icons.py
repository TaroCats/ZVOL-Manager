#!/usr/bin/env python3
"""Generate placeholder PNG icons for zvol-manager FPK application.

Generates ICON.PNG (64x64) and ICON_256.PNG (256x256).
Dark blue background with white "ZV" text.
Uses pure Python — no PIL/Pillow dependency.
"""

import struct
import zlib
import os


def create_png(width: int, height: int, output_path: str) -> None:
    """Create a minimal PNG with dark blue background and white 'ZV' text."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    # PNG signature
    signature = b"\x89PNG\r\n\x1a\n"

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)

    # Generate pixel data
    # Colors: 0=dark blue (#1a3a6b), 1=white (#ffffff)
    palette = [
        (0x1a, 0x3a, 0x6b),  # dark blue background
        (0xff, 0xff, 0xff),  # white text
    ]

    # PLTE chunk
    plte_data = b"".join(
        struct.pack("BBB", r, g, b) for r, g, b in palette
    )
    plte = chunk(b"PLTE", plte_data)

    # Create pixel map — all background by default
    rows = []
    for y in range(height):
        row = bytearray()
        row.append(0)  # filter byte (none)
        bit_buffer = 0
        bit_count = 0
        for x in range(width):
            px = 0  # default: background

            # Draw "ZV" text centered
            char_w = width // 4  # rough character region width
            char_h = height // 2
            cx1 = width // 2 - char_w
            cx2 = width // 2
            cy = height // 2 - char_h // 2

            # Simplified "Z" in left half
            if cx1 <= x < cx2 and cy <= y < cy + char_h:
                rel_x = x - cx1
                rel_y = y - cy
                # Z shape: top bar, diagonal, bottom bar
                bar_thickness = max(1, char_h // 6)
                if (rel_y < bar_thickness or
                    rel_y > char_h - bar_thickness or
                    abs(rel_y - (char_h - 1 - rel_x * (char_h - 1) / max(char_w - 1, 1))) < bar_thickness):
                    px = 1

            # Simplified "V" in right half
            if x >= cx2 and cy <= y < cy + char_h:
                rel_x = x - cx2
                rel_y = y - cy
                # V shape: two diagonal lines meeting at bottom center
                mid_x = char_w / 2
                bar_thickness = max(1, char_h // 6)
                left_line = rel_y > (char_h * rel_x / max(mid_x, 1))
                right_line = rel_y > (char_h * (char_w - 1 - rel_x) / max(char_w - mid_x, 1))
                if rel_y < char_h - 1:
                    dist_left = abs(rel_y - (char_h * rel_x / max(mid_x, 1)))
                    dist_right = abs(rel_y - (char_h * (char_w - 1 - rel_x) / max(char_w - mid_x, 1)))
                    if dist_left < bar_thickness or dist_right < bar_thickness:
                        px = 1

            bit_buffer = (bit_buffer << 1) | px
            bit_count += 1
            if bit_count == 8:
                row.append(bit_buffer)
                bit_buffer = 0
                bit_count = 0
        if bit_count > 0:
            bit_buffer <<= (8 - bit_count)
            row.append(bit_buffer)
        rows.append(bytes(row))

    raw_data = b"".join(rows)
    idat = chunk(b"IDAT", zlib.compress(raw_data))

    # IEND
    iend = chunk(b"IEND", b"")

    with open(output_path, "wb") as f:
        f.write(signature)
        f.write(ihdr)
        f.write(plte)
        f.write(idat)
        f.write(iend)


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    icon_small = os.path.join(base_dir, "ICON.PNG")
    icon_large = os.path.join(base_dir, "ICON_256.PNG")

    create_png(64, 64, icon_small)
    print(f"Created: {icon_small}")

    create_png(256, 256, icon_large)
    print(f"Created: {icon_large}")

    # Also create desktop UI icons
    ui_images_dir = os.path.join(base_dir, "app", "ui", "images")
    os.makedirs(ui_images_dir, exist_ok=True)
    create_png(64, 64, os.path.join(ui_images_dir, "icon-64.png"))
    print(f"Created: {os.path.join(ui_images_dir, 'icon-64.png')}")
    create_png(256, 256, os.path.join(ui_images_dir, "icon-256.png"))
    print(f"Created: {os.path.join(ui_images_dir, 'icon-256.png')}")


if __name__ == "__main__":
    main()

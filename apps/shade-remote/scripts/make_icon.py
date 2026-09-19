#!/usr/bin/env python3
"""Draw the app icon with nothing but the standard library.

A window with a roller shade two thirds of the way down, on the app's slate
accent. Flat shapes only; the platforms round the corners themselves.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 1024
ACCENT = (0x2B, 0x5F, 0x86)
PAPER = (0xF6, 0xF3, 0xEC)
INK = (0x1E, 0x1A, 0x12)
SKY = (0xC9, 0xE1, 0xF2)


def draw() -> bytes:
    rows = []
    frame = (176, 176, SIZE - 176, SIZE - 176)
    glass = (frame[0] + 28, frame[1] + 28, frame[2] - 28, frame[3] - 28)
    shade_bottom = glass[1] + int((glass[3] - glass[1]) * 0.62)
    bar = (glass[0] - 14, frame[1] - 26, glass[2] + 14, frame[1] + 30)
    pull = (SIZE // 2 - 22, shade_bottom - 6, SIZE // 2 + 22, shade_bottom + 34)
    mullion_x = (glass[0] + glass[2]) // 2
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            color = ACCENT
            if frame[0] <= x < frame[2] and frame[1] <= y < frame[3]:
                color = PAPER
            if glass[0] <= x < glass[2] and glass[1] <= y < glass[3]:
                color = SKY
                if abs(x - mullion_x) < 8:
                    color = PAPER
            if glass[0] <= x < glass[2] and glass[1] <= y < shade_bottom:
                color = PAPER
                if (y - glass[1]) % 96 < 6:
                    color = (0xE6, 0xE0, 0xD2)
            if bar[0] <= x < bar[2] and bar[1] <= y < bar[3]:
                color = INK
            if pull[0] <= x < pull[2] and pull[1] <= y < pull[3]:
                color = INK
            row.extend(color)
        rows.append(b"\x00" + bytes(row))
    return b"".join(rows)


def png(raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> int:
    target = Path(__file__).resolve().parents[1] / "assets" / "icon.png"
    target.write_bytes(png(draw()))
    print(f"wrote {target.name} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

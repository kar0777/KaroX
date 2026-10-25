"""Dependency-free PNG encoding, downscaling and frame quality gates.

Captures arrive as top-down 32-bit BGRX buffers from GDI. Pillow is not a
declared release dependency, so the guaranteed path is this stdlib encoder
(zlib + struct). Callers may prefer Pillow when it happens to be installed;
nothing here requires it.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, bgrx: bytes) -> bytes:
    """Encode a top-down BGRX buffer as an 8-bit RGB PNG."""

    if width <= 0 or height <= 0:
        raise ValueError("png dimensions must be positive")
    if len(bgrx) != width * height * 4:
        raise ValueError("bgrx buffer does not match the declared dimensions")
    raw = bytearray()
    stride = width * 4
    for row in range(height):
        raw.append(0)  # filter type None
        line = bgrx[row * stride : (row + 1) * stride]
        raw += b"".join(
            line[i + 2 : i + 3] + line[i + 1 : i + 2] + line[i : i + 1]
            for i in range(0, stride, 4)
        )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def downscale_bgrx(
    buffer: bytes, width: int, height: int, max_dim: int
) -> tuple[bytes, int, int]:
    """Integer box downscale so the longest side is at most ``max_dim``.

    Deterministic and allocation-bounded; this is the fallback path when
    Pillow is unavailable. Returns the buffer untouched when already small.
    """

    longest = max(width, height)
    if max_dim <= 0 or longest <= max_dim:
        return buffer, width, height
    scale = -(-longest // max_dim)  # ceil division
    out_w = max(1, width // scale)
    out_h = max(1, height // scale)
    out = bytearray(out_w * out_h * 4)
    divisor = scale * scale
    for oy in range(out_h):
        sy = oy * scale
        for ox in range(out_w):
            sx = ox * scale
            totals = [0, 0, 0, 0]
            for dy in range(scale):
                base = ((sy + dy) * width + sx) * 4
                if sy + dy >= height:
                    break
                for dx in range(scale):
                    if sx + dx >= width:
                        break
                    pixel = base + dx * 4
                    totals[0] += buffer[pixel]
                    totals[1] += buffer[pixel + 1]
                    totals[2] += buffer[pixel + 2]
                    totals[3] += buffer[pixel + 3]
            target = (oy * out_w + ox) * 4
            out[target] = totals[0] // divisor
            out[target + 1] = totals[1] // divisor
            out[target + 2] = totals[2] // divisor
            out[target + 3] = totals[3] // divisor
    return bytes(out), out_w, out_h


def frame_quality(buffer: bytes, width: int, height: int) -> dict[str, Any]:
    """Reject blank/uniform frames before they can pose as real captures.

    Samples a coarse grid (never the full buffer) and reports unique colors
    plus per-channel intensity ranges over the sampled pixels.
    """

    if width <= 0 or height <= 0 or len(buffer) < width * height * 4:
        return {"meaningful": False, "unique_colors": 0, "channel_ranges": []}
    step_x = max(1, width // 64)
    step_y = max(1, height // 64)
    seen: set[tuple[int, int, int]] = set()
    mins = [255, 255, 255]
    maxs = [0, 0, 0]
    for y in range(0, height, step_y):
        row = y * width * 4
        for x in range(0, width, step_x):
            pixel = row + x * 4
            b = buffer[pixel]
            g = buffer[pixel + 1]
            r = buffer[pixel + 2]
            if len(seen) <= 4096:
                seen.add((r, g, b))
            for index, value in enumerate((r, g, b)):
                if value < mins[index]:
                    mins[index] = value
                if value > maxs[index]:
                    maxs[index] = value
    unique = len(seen)
    ranges = [maxs[i] - mins[i] for i in range(3)]
    meaningful = unique > 4 and max(ranges) >= 8
    return {
        "meaningful": meaningful,
        "unique_colors": unique,
        "channel_ranges": ranges,
        "sample_step": [step_x, step_y],
    }


__all__ = ["downscale_bgrx", "encode_png", "frame_quality"]

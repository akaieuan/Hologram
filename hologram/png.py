"""A minimal, stdlib-only PNG reader — just enough for thumbnail drift.

Blender renders thumbnails as 8-bit, non-interlaced RGB/RGBA PNGs. This module
reads exactly that: it parses the ``IHDR``/``IDAT``/``IEND`` chunks, zlib-inflates
the image data, and undoes the per-scanline filters (None/Sub/Up/Average/Paeth).
Anything outside that envelope — interlaced, 16-bit, palette, or grayscale — is
rejected with a clear :class:`UnsupportedPNG`, which the drift check treats as a
*warning*, not a crash. No Pillow, no new dependencies.

``mean_abs_diff(a, b)`` reduces two same-size images to a single scalar in
``[0, 1]``: the mean absolute per-channel difference over R/G/B (alpha ignored),
normalised by 255. A size mismatch raises :class:`DimensionMismatch`.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# color type -> channels per pixel, for the truecolor modes we support.
_COLOR_CHANNELS = {2: 3, 6: 4}  # 2 = RGB, 6 = RGBA


class PNGError(Exception):
    """Base class for PNG read failures."""


class UnsupportedPNG(PNGError):
    """A PNG whose format is outside the supported envelope (interlaced, 16-bit,
    palette, or grayscale)."""


class DimensionMismatch(PNGError):
    """Two images compared by :func:`mean_abs_diff` differ in size."""


@dataclass
class PNGImage:
    """A decoded image: raw, unfiltered 8-bit samples in row-major order."""
    width: int
    height: int
    channels: int  # 3 (RGB) or 4 (RGBA)
    pixels: bytes  # length == width * height * channels


def read_png(path: str | Path) -> PNGImage:
    """Read and decode a PNG file. Raises :class:`PNGError` on an unsupported or
    corrupt file and ``OSError`` when the path cannot be read."""
    return decode_png(Path(path).read_bytes())


def decode_png(data: bytes) -> PNGImage:
    """Decode PNG bytes into a :class:`PNGImage`.

    Supports 8-bit, non-interlaced RGB (color type 2) and RGBA (color type 6).
    Raises :class:`UnsupportedPNG` for any other variant and :class:`PNGError`
    for a malformed stream.
    """
    if data[:8] != PNG_SIGNATURE:
        raise UnsupportedPNG("not a PNG (bad signature)")

    ihdr: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        start = pos + 8
        end = start + length
        if end > n:
            raise PNGError("truncated PNG chunk")
        chunk = data[start:end]
        if ctype == b"IHDR":
            if length != 13:
                raise PNGError("bad IHDR length")
            width, height, bit_depth, color_type, compression, filter_method, interlace = \
                struct.unpack(">IIBBBBB", chunk)
            if bit_depth != 8:
                raise UnsupportedPNG(f"unsupported bit depth {bit_depth} (need 8)")
            if color_type not in _COLOR_CHANNELS:
                raise UnsupportedPNG(
                    f"unsupported color type {color_type} (need 2=RGB or 6=RGBA)"
                )
            if compression != 0 or filter_method != 0:
                raise UnsupportedPNG("unsupported compression/filter method")
            if interlace != 0:
                raise UnsupportedPNG("interlaced PNG not supported")
            ihdr = (width, height, bit_depth, color_type, _COLOR_CHANNELS[color_type])
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
        pos = end + 4  # skip the 4-byte CRC

    if ihdr is None:
        raise PNGError("no IHDR chunk")
    width, height, _bit_depth, _color_type, channels = ihdr
    if not idat:
        raise PNGError("no IDAT data")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise PNGError(f"zlib inflate failed: {e}") from e

    pixels = _unfilter(raw, width, height, channels)
    return PNGImage(width=width, height=height, channels=channels, pixels=pixels)


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytes:
    """Reverse the PNG per-scanline filters, returning contiguous pixel bytes."""
    stride = width * channels
    expected = height * (stride + 1)  # each row carries a leading filter byte
    if len(raw) < expected:
        raise PNGError("decompressed image data too short")

    out = bytearray(height * stride)
    prev = bytearray(stride)  # zero row above the first scanline
    src = 0
    dst = 0
    for _ in range(height):
        ftype = raw[src]
        src += 1
        line = raw[src:src + stride]
        src += stride
        cur = bytearray(stride)
        if ftype == 0:  # None
            cur[:] = line
        elif ftype == 1:  # Sub
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                cur[i] = (line[i] + a) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                cur[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                cur[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                cur[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        else:
            raise PNGError(f"unknown scanline filter {ftype}")
        out[dst:dst + stride] = cur
        dst += stride
        prev = cur
    return bytes(out)


def _paeth(a: int, b: int, c: int) -> int:
    """The Paeth predictor: pick whichever of left/up/up-left is closest to the
    linear estimate ``a + b - c``."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def mean_abs_diff(a: PNGImage, b: PNGImage) -> float:
    """Mean absolute per-pixel difference over R/G/B, normalised to ``[0, 1]``.

    Alpha is ignored, so an RGB image and an RGBA image with identical color
    channels compare as identical. Raises :class:`DimensionMismatch` when the two
    images are not the same width and height.
    """
    if a.width != b.width or a.height != b.height:
        raise DimensionMismatch(
            f"{a.width}x{a.height} vs {b.width}x{b.height}"
        )
    count = a.width * a.height
    if count == 0:
        return 0.0
    pa, pb = a.pixels, b.pixels
    ca, cb = a.channels, b.channels
    total = 0
    for i in range(count):
        oa = i * ca
        ob = i * cb
        total += abs(pa[oa] - pb[ob])
        total += abs(pa[oa + 1] - pb[ob + 1])
        total += abs(pa[oa + 2] - pb[ob + 2])
    return total / (count * 3 * 255)

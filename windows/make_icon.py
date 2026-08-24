"""Generate windows/freeclaw.ico — the tray, shortcut and installer icon.

Stdlib only (zlib + struct), so the icon can be regenerated on any machine
without adding a build-time image dependency. Run it from the repo root:

    python windows/make_icon.py

The mark is three tapered talons in the app's own accent lime (#c8f04a) on a
transparent field. Transparent rather than a dark badge on purpose: this icon
spends most of its life in the notification area, where a dark badge reads as
a hole on a dark taskbar and the lime glyph reads on both Windows themes.

Small sizes get fatter, shorter talons (see SMALL_SIZE_CUTOFF). Three 1px
strokes turn to grey mush at 16x16, which is precisely the size the tray uses.
"""

import os
import struct
import zlib

ACCENT = (0xC8, 0xF0, 0x4A)          # --accent from Flask/templates/*.html
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SMALL_SIZE_CUTOFF = 24               # at or below this, use the fattened mark

# The foot pad the talons grow out of: (cx, cy, rx, ry). Without it the three
# strokes read as three separate leaves rather than one claw.
PALM = (0.50, 0.205, 0.150, 0.088)

# Each talon is a CUBIC bezier plus its stroke radius at the base and at the
# tip, in 0..1 coordinates with y pointing down. Cubic and not quadratic
# because a talon is an S: it sweeps outward from the pad, runs down, then
# hooks back inward at the point, and one control point cannot do that.
TALONS = (
    ((0.425, 0.25), (0.155, 0.34), (0.085, 0.65), (0.265, 0.86), 0.056, 0.007),
    ((0.500, 0.26), (0.478, 0.48), (0.506, 0.70), (0.500, 0.94), 0.060, 0.007),
    ((0.575, 0.25), (0.845, 0.34), (0.915, 0.65), (0.735, 0.86), 0.056, 0.007),
)


def _bezier(p0, p1, p2, p3, t):
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _talon_samples(fatten, min_radius):
    """Flatten every talon into (x, y, radius) points.

    Sampling into circles and taking a union is what gives the stroke its
    taper for free — a constant-width polyline can't narrow to a point.
    """
    points = []
    for p0, p1, p2, p3, r_start, r_tip in TALONS:
        for i in range(97):
            t = i / 96.0
            x, y = _bezier(p0, p1, p2, p3, t)
            # Taper on t**1.35 rather than t: holds the weight of the stroke
            # most of the way down, then narrows quickly into the point,
            # which is the shape of an actual claw.
            r = (r_start + (r_tip - r_start) * (t ** 1.35)) * fatten
            # The floor only applies to the small sizes. A taper that reaches a
            # true point is correct at 256px and invisible at 16px, where the
            # bottom third of every talon lands under one pixel of coverage and
            # the mark looks like it fades out halfway down.
            points.append((x, y, max(r, min_radius)))
    return points


def _coverage(px, py, points, palm):
    """True inside the mark, False outside — the union of the pad and every
    talon circle."""
    cx, cy, rx, ry = palm
    dx, dy = (px - cx) / rx, (py - cy) / ry
    if dx * dx + dy * dy <= 1.0:
        return True
    for x, y, r in points:
        dx, dy = px - x, py - y
        if dx * dx + dy * dy <= r * r:
            return True
    return False


def _render(size):
    """RGBA bytes for one square icon, supersampled for smooth edges."""
    small = size <= SMALL_SIZE_CUTOFF
    fatten = 1.5 if small else 1.0
    points = _talon_samples(fatten, 0.045 if small else 0.0)
    cx, cy, rx, ry = PALM
    palm = (cx, cy, rx * fatten, ry * fatten)
    # Small icons need the most anti-aliasing and cost the least to oversample.
    ss = 5 if size <= 32 else (3 if size <= 64 else 2)
    samples = ss * ss
    r, g, b = ACCENT

    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            hits = 0
            for sy in range(ss):
                fy = (py + (sy + 0.5) / ss) / size
                for sx in range(ss):
                    fx = (px + (sx + 0.5) / ss) / size
                    if _coverage(fx, fy, points, palm):
                        hits += 1
            alpha = int(round(255.0 * hits / samples))
            # Premultiplication is not used: ICO/PNG alpha is straight, and
            # colouring fully transparent pixels black avoids a dark fringe
            # when a viewer ignores alpha.
            row += bytes((r, g, b, alpha)) if alpha else b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


def _png(size, rows):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    # Filter type 0 (None) on every scanline. The image is flat colour plus an
    # alpha ramp, so zlib does the real work and a smarter filter buys little.
    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def build(path):
    images = [_png(s, _render(s)) for s in SIZES]

    # ICONDIR, then one 16-byte ICONDIRENTRY per image, then the PNG payloads.
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in zip(SIZES, images):
        # A 256px icon is recorded as 0 — the field is one byte wide.
        dim = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png

    with open(path, "wb") as f:
        f.write(header + entries + blobs)
    return len(header + entries + blobs)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "freeclaw.ico")
    total = build(out)
    print(f"wrote {out} ({total:,} bytes, {len(SIZES)} sizes)")

    # A PNG preview alongside it, purely so the mark can be eyeballed without
    # a Windows machine. Not shipped or referenced anywhere.
    with open(os.path.join(here, "icon-preview.png"), "wb") as f:
        f.write(_png(256, _render(256)))

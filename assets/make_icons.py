"""Generate every platform's app icon from assets/eagle.png.

Run it from a repo checkout:

    python3 assets/make_icons.py

It writes three files, and they are the only icons FreeClaw ships:

    windows/freeclaw.ico   tray, shortcut and installer icon
    mac/freeclaw.png       the menu bar icon, downsampled at runtime
    mac/freeclaw.icns      the app bundle icon (Finder, Spotlight, Login Items)

One source, three outputs, so the mark cannot drift between platforms — which
is what the two separate per-platform generators this replaced were always at
risk of. eagle.png is the same file the website serves at
https://freeclaw.eedeb.dev/eagle.png, checked in unmodified so the icons can be
rebuilt without the network.

Unlike the generator this replaces, it needs Pillow:

    pip install pillow

That constraint used to be worth avoiding — the old mark was three bezier
talons drawn from scratch, so stdlib zlib and struct could do the whole job.
A mascot with soft edges and antialiasing cannot be resampled by hand without
either writing a Lanczos filter or accepting visibly worse icons, and Pillow is
already a dependency of both the macOS and Windows installs. Nobody installing
FreeClaw needs it: the outputs are checked in.
"""

import os
import struct
import sys

try:
    from PIL import Image
except ImportError:
    raise SystemExit(
        "This needs Pillow:  pip install pillow\n"
        "(Only to regenerate the icons — the generated files are checked in.)")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SOURCE = os.path.join(HERE, "eagle.png")

# Windows' .ico carries every size the shell asks for, from the 16px notification
# area up to the 256px "extra large icons" view in Explorer.
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# The menu bar draws at 22 *points*, which is 44 pixels on Retina; mac/tray.py
# downsamples this file to whatever the bar actually reports. Kept comfortably
# larger than either so the runtime resize always has pixels to work with.
MENU_BAR_PNG = 128

# icns type -> pixel size. The "@2x" types (ic11-ic14) are what Retina draws;
# the 1x types beside them cover the few places macOS still renders at 1x.
# Stops at 512: the 1024px slot is reached only by Finder at its largest zoom
# and by Quick Look, neither of which is where a menu bar utility is looked at.
ICNS_TYPES = (
    (b"ic11", 32),     # 16pt @2x
    (b"ic12", 64),     # 32pt @2x
    (b"ic07", 128),    # 128pt @1x
    (b"ic13", 256),    # 128pt @2x
    (b"ic08", 256),    # 256pt @1x
    (b"ic14", 512),    # 256pt @2x
    (b"ic09", 512),    # 512pt @1x
)

# How much of each canvas the bird occupies. The menu bar gets the most room
# around it: an icon that runs edge to edge there sits tighter against the
# clock and the Control Center items than anything else in the bar, and reads
# as crowding rather than as a bigger icon.
MENU_BAR_INSET = 0.88
APP_ICON_INSET = 0.96


def load_mark():
    """The eagle, trimmed of its transparent margin and centred on a square.

    Trimmed because the source carries up to 68px of empty pixels on one edge
    and 17 on another, and at 16 pixels wide every one of those is a pixel the
    bird does not get. Squared afterwards so no output has to distort it — the
    trimmed mark is 1218x1150, near enough square that the padding is slight.
    """
    image = Image.open(SOURCE).convert("RGBA")
    box = image.getchannel("A").getbbox()
    if box is None:
        raise SystemExit(f"{SOURCE} is fully transparent.")
    trimmed = image.crop(box)
    side = max(trimmed.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(trimmed, ((side - trimmed.width) // 2,
                           (side - trimmed.height) // 2))
    return square


def render(mark, size, inset):
    """One square icon, the mark centred at `inset` of the canvas."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = max(1, int(round(size * inset)))
    scaled = mark.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    canvas.paste(scaled, (offset, offset))
    return canvas


def build_ico(mark, path):
    """Pillow writes a multi-size .ico in one call, compressing the large
    entries as PNG, which is what Windows has read since Vista."""
    largest = render(mark, max(ICO_SIZES), APP_ICON_INSET)
    largest.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return os.path.getsize(path)


def build_icns(mark, path):
    """An .icns holding one PNG per size in ICNS_TYPES.

    The format is as simple as it looks: an 8-byte header ('icns' plus the
    total file length), then one entry per image — a 4-byte type, a length that
    *includes* those 8 bytes, and the payload. Every type used here takes a PNG
    directly, so nothing has to be packed into the old 1-bit mask formats.
    """
    import io

    rendered = {}
    entries = b""
    for icns_type, size in ICNS_TYPES:
        if size not in rendered:
            buffer = io.BytesIO()
            render(mark, size, APP_ICON_INSET).save(buffer, "png")
            rendered[size] = buffer.getvalue()
        png = rendered[size]
        entries += icns_type + struct.pack(">I", len(png) + 8) + png

    blob = b"icns" + struct.pack(">I", len(entries) + 8) + entries
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def main():
    if not os.path.exists(SOURCE):
        raise SystemExit(f"{SOURCE} not found — this needs a repo checkout.")
    mark = load_mark()
    print(f"source: {SOURCE} (trimmed and squared to {mark.width}x{mark.height})")

    ico = os.path.join(REPO, "windows", "freeclaw.ico")
    print(f"wrote {ico} ({build_ico(mark, ico):,} bytes, {len(ICO_SIZES)} sizes)")

    png = os.path.join(REPO, "mac", "freeclaw.png")
    render(mark, MENU_BAR_PNG, MENU_BAR_INSET).save(png)
    print(f"wrote {png} ({os.path.getsize(png):,} bytes, "
          f"{MENU_BAR_PNG}x{MENU_BAR_PNG})")

    icns = os.path.join(REPO, "mac", "freeclaw.icns")
    print(f"wrote {icns} ({build_icns(mark, icns):,} bytes, "
          f"{len(ICNS_TYPES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

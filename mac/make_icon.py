"""Generate mac/freeclaw.png and mac/freeclaw.icns — the menu bar and app icons.

Run it from a repo checkout:

    python3 mac/make_icon.py

The mark itself is *not* defined here. It lives in windows/make_icon.py, and
this script loads that module by path and only repackages what it renders:
a single PNG for the menu bar and an .icns for the app bundle. One mark, two
platforms, no chance of the two drifting apart — which is also why this is a
developer tool run from a checkout rather than something an install can do:
a macOS install doesn't check out windows/.

Why a big PNG rather than one sized for the menu bar: the status bar is 22
*points* tall, which is 44 pixels on every Retina Mac. mac/tray.py downsamples
this 128px render to whatever the bar actually asks for, so the icon stays
sharp on a display this script knows nothing about.

Why no rounded-rect background on the .icns, which is the macOS house style:
the same reason the Windows icon has no dark badge. The mark is a lime glyph
on transparency, and it has to read on a light menu bar and a dark one.
"""

import importlib.util
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# The menu bar never needs more than 44px (22pt at 2x), but the file is also
# what tray.py hands PIL, and downsampling from a comfortably larger render is
# what keeps the talons clean at every bar height macOS might report.
MENU_BAR_PNG_SIZE = 128

# icns type -> pixel size. The "@2x" types (ic11-ic14) are what Retina
# actually draws; the 1x types beside them are for the few places macOS still
# renders at 1x, and for anything reading the file that isn't macOS.
#
# It stops at 512, so the ic10 slot (512pt @2x, a 1024px image) is left out
# and macOS upscales for it. That slot is reached by Finder at its largest
# icon zoom and by Quick Look, and nowhere a menu bar utility is normally
# looked at. The reason to care: the renderer is a pure-Python supersampler
# whose cost is quadratic in the size, so 1024 alone takes longer than every
# other size here put together.
ICNS_TYPES = (
    (b"ic11", 32),     # 16pt @2x
    (b"ic12", 64),     # 32pt @2x
    (b"ic07", 128),    # 128pt @1x
    (b"ic13", 256),    # 128pt @2x
    (b"ic08", 256),    # 256pt @1x
    (b"ic14", 512),    # 256pt @2x
    (b"ic09", 512),    # 512pt @1x
)


def _load_mark():
    """Import windows/make_icon.py as a module, by path.

    Not a package import: windows/ has no __init__.py and is not meant to be
    one — it is a directory of Windows install files that happens to hold the
    one renderer both platforms share.
    """
    path = os.path.join(REPO, "windows", "make_icon.py")
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found.\n"
            "This script renders the shared mark from the Windows icon "
            "generator, so it needs a full repo checkout — not an install.")
    spec = importlib.util.spec_from_file_location("_freeclaw_mark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_icns(mark, path):
    """Write an .icns holding one PNG per size in ICNS_TYPES.

    The format is as simple as it looks: an 8-byte header ('icns' plus the
    total file length), then one entry per image — a 4-byte type, a length
    that *includes* those 8 bytes, and the payload. Every type used here takes
    a PNG directly, so nothing has to be packed into the old 1-bit mask
    formats and this stays stdlib-only, exactly as the Windows generator is.
    """
    # Cached by size, because two pairs of types above ask for the same
    # pixels (ic13/ic08 at 256, ic14/ic09 at 512) and rendering is the slow
    # part of this script by a wide margin.
    rendered = {}
    entries = b""
    for icns_type, size in ICNS_TYPES:
        if size not in rendered:
            rendered[size] = mark._png(size, mark._render(size))
        png = rendered[size]
        entries += icns_type + struct.pack(">I", len(png) + 8) + png

    blob = b"icns" + struct.pack(">I", len(entries) + 8) + entries
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def build_png(mark, path, size=MENU_BAR_PNG_SIZE):
    png = mark._png(size, mark._render(size))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


if __name__ == "__main__":
    mark = _load_mark()

    png_path = os.path.join(HERE, "freeclaw.png")
    size = build_png(mark, png_path)
    print(f"wrote {png_path} ({size:,} bytes, "
          f"{MENU_BAR_PNG_SIZE}x{MENU_BAR_PNG_SIZE})")

    icns_path = os.path.join(HERE, "freeclaw.icns")
    total = build_icns(mark, icns_path)
    print(f"wrote {icns_path} ({total:,} bytes, {len(ICNS_TYPES)} sizes)")

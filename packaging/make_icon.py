"""Generate the macOS app icon as AppIcon.icns.

From packaging/icon_source.png (the Niti logo) when it is there — see
_master_from_source for how the opaque render becomes a proper icon — else the drawn
"smart envelope": a rounded teal gradient tile, a white envelope, a small accent spark.
Renders a 1024px master, downsamples to the standard iconset sizes, and runs
`iconutil` to produce the .icns.

Usage:  python packaging/make_icon.py /path/to/AppIcon.icns
Requires Pillow (the project's `build` extra) and macOS `iconutil`.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ACCENT_TOP = (24, 196, 154)    # teal-green (matches app --accent)
ACCENT_BOT = (9, 132, 104)
WHITE = (255, 255, 255)
FLAP = (208, 214, 220)
SPARK = (255, 214, 102)        # warm "smart" accent


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = round(ACCENT_TOP[0] + (ACCENT_BOT[0] - ACCENT_TOP[0]) * t)
        g = round(ACCENT_TOP[1] + (ACCENT_BOT[1] - ACCENT_TOP[1]) * t)
        b = round(ACCENT_TOP[2] + (ACCENT_BOT[2] - ACCENT_TOP[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return img


def _spark(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color) -> None:
    # 4-point star (sparkle).
    w = r * 0.32
    draw.polygon(
        [(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
         (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)],
        fill=color,
    )


def render_master(size: int = 1024) -> Image.Image:
    s = size
    # Rounded tile from the gradient via an alpha mask.
    tile = _gradient(s).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.225), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(tile, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # Envelope body.
    ex0, ey0, ex1, ey1 = s * 0.20, s * 0.32, s * 0.80, s * 0.70
    d.rounded_rectangle([ex0, ey0, ex1, ey1], radius=int(s * 0.045), fill=WHITE)
    # Flap (the classic "V"), drawn as thick lines.
    mid = ((ex0 + ex1) / 2, ey0 + (ey1 - ey0) * 0.46)
    lw = max(2, int(s * 0.012))
    d.line([(ex0, ey0), mid], fill=FLAP, width=lw)
    d.line([(ex1, ey0), mid], fill=FLAP, width=lw)
    # Smart accent spark, top-right over the tile.
    _spark(d, s * 0.74, s * 0.28, s * 0.11, SPARK)
    _spark(d, s * 0.625, s * 0.20, s * 0.045, WHITE)
    return img


# (name, pixel size) for Contents.iconset
_SIZES = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]


# The Niti logo (packaging/icon_source.png, a copy of «Niti logo.png») wins over the
# drawn envelope. It is an opaque render: a light rounded tile on black, with a black
# margin around it — pasted as is, the Dock shows a black square with the tile inside.
# So: find the tile, fit it into Apple's icon grid (an 824 px rounded square on a 1024 px
# transparent canvas, corner radius ≈ 22.5 %), and cut the corners with that mask. The
# mask is rounder than the tile's own corners, so no black survives under it.
_SOURCE_PNG = Path(__file__).with_name("icon_source.png")
_CANVAS, _TILE = 1024, 824


def _master_from_source() -> Image.Image | None:
    if not _SOURCE_PNG.exists():
        return None
    img = Image.open(_SOURCE_PNG).convert("RGBA")
    # The tile is the light region; everything darker than mid-grey is the black margin
    # (the logo's own navy glyph lies inside the tile, so the bbox is still the tile's).
    bbox = img.convert("L").point(lambda v: 255 if v > 128 else 0).getbbox()
    if bbox:
        # Step inside the bbox: its outermost pixels are the tile's edge blended with
        # the black margin, and kept they draw a dark hairline around the icon.
        inset = max(2, round((bbox[2] - bbox[0]) * 0.008))
        img = img.crop((bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset))
    w, h = img.size
    if w != h:  # the tile is square; a pixel of slack from the threshold is cropped away
        m = min(w, h)
        img = img.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m))
    tile = img.resize((_TILE, _TILE), Image.LANCZOS)
    mask = Image.new("L", (_TILE, _TILE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, _TILE - 1, _TILE - 1], radius=int(_TILE * 0.225), fill=255
    )
    master = Image.new("RGBA", (_CANVAS, _CANVAS), (0, 0, 0, 0))
    off = (_CANVAS - _TILE) // 2
    master.paste(tile, (off, off), mask)
    return master


def build_icns(out_path: Path) -> None:
    master = _master_from_source() or render_master(1024)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for name, px in _SIZES:
            master.resize((px, px), Image.LANCZOS).save(iconset / name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(out_path)], check=True
        )


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("packaging/AppIcon.icns")
    build_icns(out)
    print(f"wrote {out}")

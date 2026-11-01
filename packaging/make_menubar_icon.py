"""Generate the macOS menu-bar status icon — the Niti "N" monogram — as a template PNG.

The menu bar wants a small monochrome *template* image (black shape + alpha) so macOS
tints it for light/dark menu bars. We extract the dark navy "N" from static/logo.png
(a navy monogram on a white rounded tile, itself on a black PNG background): flood-fill
the black background in from the corners, then keep the dark pixels that sit inside the
white tile. The result is trimmed to the glyph, padded, and centred in a square.

Usage:  python packaging/make_menubar_icon.py /path/to/MenuBarIcon.png
Requires Pillow (the project's `build` extra).
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

SRC = Path(__file__).resolve().parent.parent / "static" / "logo.png"
OUT_PX = 36           # stored bitmap size (18pt @2x); Swift sets NSImage.size = 18
PAD_FRAC = 0.06       # padding around the glyph inside the square


def _lum(p) -> int:
    return (p[0] * 299 + p[1] * 587 + p[2] * 114) // 1000


def extract_glyph(src: Path) -> Image.Image:
    im = Image.open(src).convert("RGBA")
    w, h = im.size
    px = im.load()

    # Flood-fill the (dark) background inward from the four corners. The white tile
    # blocks it, so the equally-dark N — enclosed by the tile — is never reached.
    bg = [[False] * w for _ in range(h)]
    q: deque[tuple[int, int]] = deque()
    for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        q.append((cx, cy))
    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or bg[y][x]:
            continue
        r, g, b, a = px[x, y]
        # Stop only on the bright white tile. A high threshold also consumes the
        # anti-aliased black->white ring at the tile edge, so it leaves no ghost outline.
        if a > 10 and _lum((r, g, b)) >= 170:
            continue
        bg[y][x] = True
        q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    # Alpha = darkness, but only for foreground (non-background) pixels. The navy N
    # becomes opaque, the white tile fully transparent, edges anti-aliased.
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    op = out.load()
    x0, y0, x1, y1 = w, h, 0, 0
    for y in range(h):
        for x in range(w):
            if bg[y][x]:
                continue
            lum = _lum(px[x, y])
            # Solid for the navy N, anti-aliased ramp to transparent toward the tile.
            a = max(0, min(255, round((150 - lum) / 60 * 255)))
            if a <= 4:
                continue
            op[x, y] = (0, 0, 0, a)
            x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)

    if x0 > x1:
        raise SystemExit("no glyph pixels found — check the source logo")
    return out.crop((x0, y0, x1 + 1, y1 + 1))


def build(out_path: Path) -> None:
    glyph = extract_glyph(SRC)
    gw, gh = glyph.size
    inner = round(OUT_PX * (1 - 2 * PAD_FRAC))
    scale = inner / max(gw, gh)
    glyph = glyph.resize((max(1, round(gw * scale)), max(1, round(gh * scale))), Image.LANCZOS)
    gw, gh = glyph.size
    canvas = Image.new("RGBA", (OUT_PX, OUT_PX), (0, 0, 0, 0))
    canvas.alpha_composite(glyph, ((OUT_PX - gw) // 2, (OUT_PX - gh) // 2))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("packaging/MenuBarIcon.png")
    build(out)
    print(f"wrote {out}")

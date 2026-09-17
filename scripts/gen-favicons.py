#!/usr/bin/env python
"""Regenerate static/favicon/* from the brand master.

Run this whenever the logo changes; do not hand-edit the icons. Every output is
a downscale of one source, so the browser tab, the iOS home screen and the PWA
install prompt cannot drift apart the way they had -- the favicons were still
the BrightBean *Studio* mark long after the app itself had been rebranded.

    .venv/bin/python scripts/gen-favicons.py [source]

Two families, and the difference matters:

* The plain icons stay transparent, so a browser can sit them on whatever
  colour its tab strip happens to be.
* The three the webmanifest declares `"purpose": "maskable"` are handed to the
  OS, which crops them to its own circle/squircle and keeps roughly the middle
  80%. They therefore get a full-bleed plate and the mark inset into that safe
  zone -- full-bleed art would lose the wordmark to the crop.

Nothing is ever upscaled: an icon larger than the source is skipped and
reported, because a blurry 512 shipped to an install prompt is worse than an
obviously stale one. Pass a bigger source to fill those in.

`favicon.svg` is not vector art. It is a raster wrapped in an <svg>, which is
how the original was built; this keeps that shape rather than pretending to a
vector the brand does not have.
"""

import base64
import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "static" / "img"
OUT = ROOT / "static" / "favicon"

# Preferred first. The -small export only reaches 128, so it cannot fill the
# maskable sizes; the full-size master can.
SOURCES = [IMG / "brightbean-chat-logo.webp", IMG / "brightbean-chat-logo-small.png"]

# The plate behind the maskable icons. Matches `theme_color` in site.webmanifest,
# so the icon and the OS chrome around it are the same colour.
PLATE = (255, 218, 153, 255)
SAFE_ZONE = 0.80

# The art is flat cartoon vector -- a few dozen large blocks of colour -- so an
# adaptive 256-entry palette is visually indistinguishable from RGBA at roughly
# a third of the bytes. FASTOCTREE because it is the only Pillow quantiser that
# takes an alpha channel (MEDIANCUT refuses; libimagequant is not built in).
#
# The .ico is the deliberate exception and stays RGBA. Quantising snaps
# part-transparent edge pixels to fully transparent, which is invisible at 96px
# but chews visible notches out of a 16px frame, where nearly every pixel is an
# edge. That is the size a browser tab actually shows, and the ~3 KB saved is
# not worth it.
PALETTE_COLOURS = 256

# Trimming transparent padding can leave the art a pixel or two under a target
# (a 128px export crops to 127), and refusing that would skip an icon over
# rounding noise rather than over real softness.
UPSCALE_TOLERANCE = 1.02


def fit(img: Image.Image, box: int) -> Image.Image:
    """Scale to fit a `box`-square by the longest edge, centred, transparent rest.

    By longest edge rather than stretching to square: the mark is taller than it
    is wide once the wordmark is under the bean, and a stretch would smear it.
    """
    scale = box / max(img.size)
    w, h = (max(1, round(d * scale)) for d in img.size)
    canvas = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    canvas.alpha_composite(img.resize((w, h), Image.LANCZOS), ((box - w) // 2, (box - h) // 2))
    return canvas


def maskable(img: Image.Image, size: int) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), PLATE)
    inner = round(size * SAFE_ZONE)
    offset = (size - inner) // 2
    canvas.alpha_composite(fit(img, inner), (offset, offset))
    return canvas


def squeeze(img: Image.Image, *, keep: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Quantise, unless that somehow costs bytes on this particular image.

    `keep` pins one colour that has to survive exactly. Quantising nudges the
    plate off by a step in each channel (#FFDA99 -> #FED998), which no eye will
    catch, but the manifest's `theme_color` is the literal string #ffda99 and
    the OS paints its chrome with it right up against the icon. Snapping the
    entry back keeps the two provably identical instead of merely close.
    """
    palette = img.quantize(colors=PALETTE_COLOURS, method=Image.FASTOCTREE)
    if keep is not None:
        entries = palette.getpalette("RGBA") or []
        nearest = min(
            range(len(entries) // 4),
            key=lambda i: sum((entries[i * 4 + c] - keep[c]) ** 2 for c in range(4)),
        )
        entries[nearest * 4 : nearest * 4 + 4] = list(keep)
        palette.putpalette(entries, rawmode="RGBA")
    return palette if _png_bytes(palette) < _png_bytes(img) else img


def _png_bytes(img: Image.Image) -> int:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, compress_level=9)
    return len(buf.getvalue())


def write_png(img: Image.Image, path: Path, *, keep=None) -> None:
    squeeze(img, keep=keep).save(path, format="PNG", optimize=True, compress_level=9)


def _display(path: Path) -> str:
    """Repo-relative when it can be, absolute when it cannot.

    The CLI takes a source from anywhere -- that is the whole point of passing
    a bigger master than the one committed -- so this cannot assume the file
    sits under ROOT the way the committed defaults do.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    override = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    source = override or next((p for p in SOURCES if p.exists()), None)
    if source is None or not source.exists():
        sys.exit(f"no source image found; looked for {', '.join(str(p) for p in SOURCES)}")

    master = Image.open(source).convert("RGBA")
    alpha = master.getchannel("A")
    if alpha.getextrema()[0] == 255:
        sys.exit(
            f"{source.name} is fully opaque -- it has a background baked in. The "
            "plain favicons need transparency; key the background out first."
        )
    master = master.crop(alpha.getbbox())  # trim export padding so sizes are comparable
    ceiling = max(master.size)
    print(f"source: {_display(source)} ({ceiling}px)\n")

    skipped = []

    def guard(size: int) -> bool:
        if size > ceiling * UPSCALE_TOLERANCE:
            skipped.append(size)
            return False
        return True

    if guard(96):
        write_png(fit(master, 96), OUT / "favicon-96x96.png")
    if guard(48):
        fit(master, 48).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    if guard(128):
        buf = io.BytesIO()
        squeeze(fit(master, 128)).save(buf, format="PNG", optimize=True, compress_level=9)
        b64 = base64.b64encode(buf.getvalue()).decode()
        (OUT / "favicon.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" width="128" height="128" '
            'viewBox="0 0 128 128"><image width="128" height="128" '
            f'xlink:href="data:image/png;base64,{b64}"/></svg>'
        )

    # Names and sizes are fixed by site.webmanifest and base.html.
    for name, size in [
        ("apple-touch-icon.png", 180),
        ("web-app-manifest-192x192.png", 192),
        ("web-app-manifest-512x512.png", 512),
    ]:
        if guard(size):
            write_png(maskable(master, size), OUT / name, keep=PLATE)

    for f in sorted(OUT.iterdir()):
        if f.suffix != ".webmanifest":
            print(f"  {f.name:32s} {f.stat().st_size:>8,d} B")

    if skipped:
        print(
            f"\nSKIPPED (would upscale a {ceiling}px source): "
            f"{', '.join(str(s) for s in sorted(skipped))}. "
            f"These still hold the old art. Supply a larger source to fill them in."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

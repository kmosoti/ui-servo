#!/usr/bin/env python3
"""Generate the PWA icon set from the site's own mark — no randomness.

`site/assets/favicon.svg` is the "KM" box mark: a bordered square with "KM"
centered inside it. The sticky header (`portfolio_header` in
`site/src/layout.rs`) carries a second piece of the same identity — a small
ember dot beside the "KM" wordmark. This script draws the two together: the
favicon's box, the header's dot, "KM" in the site's own JetBrains Mono, on the
site's near-black background with its ember accent — at icon scale, four
sizes, deterministically.

PIL's FreeType binding turned out to read the vendored variable-weight
`jetbrains-mono-700-latin.woff2` directly (checked by hand: `ImageFont.
truetype` loads it and renders correct glyphs at the default instance), so the
"faithful geometric echo" fallback described in the unit brief was not needed
— the mark uses the real webfont.

Every measurement here is a fraction of the output size, so the four icons are
the same drawing at four scales, not four separate drawings. Run with
`uv run python tools/make_icons.py`; output lands in `site/assets/icons/` and
is committed like the other vendored artifacts (htmx.min.js, the wasm island
binaries) — nobody should need Pillow installed just to build the site.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "site" / "assets" / "fonts" / "jetbrains-mono-700-latin.woff2"
OUT_DIR = ROOT / "site" / "assets" / "icons"

# The site's own palette (direction/direction.toml — see tokens.css / layout.rs).
BG = (0x08, 0x09, 0x0B, 255)  # --color-bg / portfolio_shell body background
EMBER = (0xFF, 0x7A, 0x45, 255)  # ember accent — box border, brand dot
INK = (0xEC, 0xE7, 0xDD, 255)  # cream text colour used for "KM" in the header


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def _mark(size: int, *, box_fraction: float, round_bg: bool) -> Image.Image:
    """Draw the box + dot + "KM" mark, scaled so it fills `box_fraction` of
    `size` at its widest, centred on a `size`x`size` canvas.

    `box_fraction` is the knob the two safe-zone shapes use: 0.75 for the
    regular icons (an inset box, echoing the favicon's 48/64 = 0.75 border),
    0.34 for the maskable icon, whose ink must stay inside a centred 40%
    square — kept well under that with room for the stroke to bleed outward.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bg_radius = round(size * 0.22) if round_bg else 0
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=bg_radius, fill=BG)

    box = round(size * box_fraction)
    half = box / 2
    cx = cy = size / 2
    box_xy = [cx - half, cy - half, cx + half, cy + half]
    stroke = max(1, round(size * box_fraction * 0.045))
    box_radius = round(box * 0.14)
    draw.rounded_rectangle(box_xy, radius=box_radius, outline=EMBER, width=stroke)

    # "KM" in the site's own webfont, sized relative to the box like the
    # favicon's 16/48 (~0.33 of the box).
    font = _font(round(box * 0.34))
    text = "KM"
    text_w = draw.textlength(text, font=font)

    # The header's brand dot, laid out to the left of the wordmark with a
    # small gap — the same [dot] [gap] [KM] arrangement portfolio_header uses.
    dot_d = max(2, round(box * 0.11))
    gap = round(box * 0.09)
    group_w = dot_d + gap + text_w
    start_x = cx - group_w / 2

    dot_xy = [start_x, cy - dot_d / 2, start_x + dot_d, cy + dot_d / 2]
    draw.ellipse(dot_xy, fill=EMBER)

    text_x = start_x + dot_d + gap
    draw.text((text_x, cy), text, font=font, fill=INK, anchor="lm")

    return img


def _assert_maskable_safe(img: Image.Image) -> None:
    """Fail loudly if any non-transparent, non-background pixel lands outside
    the centred 40% safe zone a maskable icon promises to keep clear."""
    size = img.width
    lo, hi = size * 0.3, size * 0.7
    px = img.load()
    for y in range(size):
        for x in range(size):
            r, g, b, a = px[x, y]
            if a == 0 or (r, g, b, a) == BG:
                continue
            if not (lo <= x <= hi and lo <= y <= hi):
                raise AssertionError(
                    f"maskable ink at ({x},{y}) is outside the central 40% "
                    f"safe zone [{lo:.0f},{hi:.0f}]"
                )


def _flatten(img: Image.Image) -> Image.Image:
    """Composite onto opaque BG, dropping the alpha channel.

    Only for the two icons that platform rules say must be edge-to-edge and
    opaque: a maskable icon's corners are cropped by whatever shape the OS
    picks, so transparency there is undefined per-platform; an apple-touch
    icon's alpha channel is ignored outright, and iOS applies its own
    rounding. The "any"-purpose icons below are saved as RGBA instead — their
    rounded corners are real transparency, not just drawn to look that way."""
    flat = Image.new("RGB", img.size, BG[:3])
    flat.paste(img, mask=img.split()[3])
    return flat


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # "any" purpose: RGBA, corners genuinely transparent outside the rounded
    # background — round_bg's rounded_rectangle mask is the alpha channel a
    # browser actually sees, not a shape drawn onto an opaque fill it would be
    # indistinguishable from.
    icon_192 = _mark(192, box_fraction=0.75, round_bg=True)
    icon_192.save(OUT_DIR / "icon-192.png")

    icon_512 = _mark(512, box_fraction=0.75, round_bg=True)
    icon_512.save(OUT_DIR / "icon-512.png")

    # Sizes must match manifest.webmanifest's icons[].sizes — both are hand-
    # authored from these same four constants, so a change to one is a change
    # to the other.
    maskable = _mark(512, box_fraction=0.34, round_bg=False)
    _assert_maskable_safe(maskable)
    _flatten(maskable).save(OUT_DIR / "icon-512-maskable.png")

    apple = _mark(180, box_fraction=0.75, round_bg=False)
    _flatten(apple).save(OUT_DIR / "apple-touch-180.png")

    for name in ("icon-192.png", "icon-512.png", "icon-512-maskable.png", "apple-touch-180.png"):
        print(f"wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()

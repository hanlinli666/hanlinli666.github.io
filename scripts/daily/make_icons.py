#!/usr/bin/env python3
"""Regenerate the PWA icon set (only needed when you want to change the logo).

    python scripts/daily/make_icons.py

Requires Pillow, which is a *local* convenience only - the daily pipeline on
GitHub Actions never runs this file.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "daily" / "icons"

C1 = (99, 102, 241)   # indigo
C2 = (20, 184, 166)   # teal
SS = 4                # supersampling factor


def linear_gradient(size: int) -> Image.Image:
    """45° diagonal gradient, computed small then upscaled for smoothness."""
    small = Image.new("RGB", (96, 96))
    px = small.load()
    for y in range(96):
        for x in range(96):
            t = (x + y) / (2 * 95)
            px[x, y] = tuple(round(C1[i] + (C2[i] - C1[i]) * t) for i in range(3))
    return small.resize((size, size), Image.BICUBIC)


def add_glow(img: Image.Image) -> Image.Image:
    """Soft highlight in the top-left + a darker bottom-right corner."""
    size = img.width
    glow = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(glow)
    r = int(size * 0.52)
    d.ellipse([-int(size * 0.22), -int(size * 0.28), r, r], fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.14))

    white = Image.new("RGB", (size, size), (255, 255, 255))
    img = Image.composite(white, img, glow.point(lambda v: v // 2))

    shade = Image.new("L", (size, size), 0)
    d2 = ImageDraw.Draw(shade)
    d2.ellipse(
        [int(size * 0.55), int(size * 0.55), int(size * 1.35), int(size * 1.35)],
        fill=80,
    )
    shade = shade.filter(ImageFilter.GaussianBlur(size * 0.16))
    black = Image.new("RGB", (size, size), (12, 14, 24))
    return Image.composite(black, img, shade.point(lambda v: v // 3))


def draw_mark(img: Image.Image, scale: float = 1.0) -> Image.Image:
    """Three ascending rounded bars, centred, within the requested scale."""
    size = img.width
    draw = ImageDraw.Draw(img, "RGBA")
    unit = size / 100.0

    bar_w = 9.0 * unit * scale
    gap = 7.0 * unit * scale
    group_w = bar_w * 3 + gap * 2
    left = (size - group_w) / 2
    base_y = size / 2 + 18.0 * unit * scale
    heights = [26.0, 40.0, 54.0]

    for i, h in enumerate(heights):
        x0 = left + i * (bar_w + gap)
        y0 = base_y - h * unit * scale
        alpha = 205 + i * 25
        radius = bar_w / 2
        draw.rounded_rectangle(
            [x0, y0, x0 + bar_w, base_y],
            radius=radius,
            fill=(255, 255, 255, min(alpha, 255)),
        )
    return img


def rounded(img: Image.Image, radius_ratio: float) -> Image.Image:
    size = img.width
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255
    )
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def build(size: int, *, rounded_corners: bool, mark_scale: float) -> Image.Image:
    big = size * SS
    img = linear_gradient(big)
    img = add_glow(img)
    img = draw_mark(img, scale=mark_scale)
    if rounded_corners:
        img = rounded(img, 0.225)
    return img.resize((size, size), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("icon-192.png", 192, True, 1.0),
        ("icon-512.png", 512, True, 1.0),
        ("icon-512-maskable.png", 512, False, 0.66),
        ("apple-touch-icon.png", 180, False, 0.78),
    ]
    for name, size, rounded_corners, scale in jobs:
        icon = build(size, rounded_corners=rounded_corners, mark_scale=scale)
        target = OUT / name
        icon.save(target, "PNG", optimize=True)
        print(f"wrote {target.relative_to(ROOT)} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

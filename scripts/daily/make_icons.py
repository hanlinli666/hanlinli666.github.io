#!/usr/bin/env python3
"""Regenerate the PWA icon set from daily/icons/favicon.svg.

    python scripts/daily/make_icons.py

The SVG is the single source of truth; a headless Chromium (Edge or Chrome)
rasterises it at every size. This is a *local* convenience only - the daily
pipeline on GitHub Actions never runs this file, the PNGs are committed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "daily" / "icons" / "favicon.svg"
OUT = ROOT / "daily" / "icons"

# name, pixel size, mark scale (maskable/apple need padding), full-bleed square
JOBS = [
    ("icon-192-v2.png", 192, 1.0, False),
    ("icon-512-v2.png", 512, 1.0, False),
    ("icon-512-maskable-v2.png", 512, 0.70, True),
    ("apple-touch-icon-v2.png", 180, 0.80, True),
]

CANDIDATES = [
    os.environ.get("CHROME"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "msedge",
    "chrome",
    "chromium",
    "google-chrome",
]


def find_browser() -> str:
    for candidate in CANDIDATES:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        if Path(candidate).exists():
            return candidate
    raise SystemExit("No Chrome/Edge found; set CHROME to the executable path.")


def page(svg: str, size: int, scale: float, bleed: bool) -> str:
    if scale != 1.0:
        svg = svg.replace(
            '<g id="mark">',
            f'<g id="mark" transform="translate(32 32) scale({scale}) translate(-32 -32)">',
        )
    if bleed:
        svg = svg.replace('rx="15"', 'rx="0"')
    return (
        "<!doctype html><meta charset=\"utf-8\">"
        "<style>html,body{margin:0;padding:0;background:transparent;overflow:hidden}"
        f"svg{{display:block;width:{size}px;height:{size}px}}</style>"
        f"{svg}"
    )


def main() -> None:
    browser = find_browser()
    svg = SRC.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name, size, scale, bleed in JOBS:
            html = tmp_path / f"{Path(name).stem}.html"
            html.write_text(page(svg, size, scale, bleed), encoding="utf-8")
            target = OUT / name
            subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    f"--user-data-dir={tmp_path / 'profile'}",
                    "--default-background-color=00000000",
                    f"--screenshot={target}",
                    f"--window-size={size},{size}",
                    html.as_uri(),
                ],
                check=True,
                capture_output=True,
            )
            print(f"wrote {target.relative_to(ROOT)} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

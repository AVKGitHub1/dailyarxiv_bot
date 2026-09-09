"""Rasterize static/favicon.svg into the multi-size static/favicon.ico.

favicon.svg is the source of truth; the .ico exists only for browsers that do
not render SVG icons (notably Safari) and for the /favicon.ico probe. Re-run
this after editing the SVG, then commit both files:

    python -m pip install playwright
    python scripts/make_favicon_ico.py

Development-only: it needs playwright, which the application never imports and
which is not in requirements.txt.
"""

import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"
SIZES = [16, 32, 48, 64, 256]


def render(svg):
    frames = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        page = browser.new_page()
        for size in SIZES:
            sized = svg.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
            page.set_content(f'<body style="margin:0;background:transparent">{sized}</body>')
            # omit_background keeps the rounded corners transparent, not white.
            frames[size] = page.locator("svg").screenshot(omit_background=True)
        browser.close()
    return frames


def build_ico(frames):
    header = struct.pack("<HHH", 0, 1, len(SIZES))  # reserved, type=icon, image count
    offset = len(header) + 16 * len(SIZES)
    entries, blobs = b"", b""
    for size in SIZES:
        data = frames[size]
        dimension = size if size < 256 else 0  # 0 encodes 256 in an ICONDIRENTRY
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main():
    ico = build_ico(render(STATIC.joinpath("favicon.svg").read_text(encoding="utf-8")))
    STATIC.joinpath("favicon.ico").write_bytes(ico)
    print(f"Wrote {STATIC / 'favicon.ico'} ({len(ico)} bytes, sizes {SIZES}).")


if __name__ == "__main__":
    main()

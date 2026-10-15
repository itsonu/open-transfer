"""Generate the desktop app icons from the web app icon (needs Pillow).

    python scripts/make_icons.py [--out build/icons]

Writes:

    open-transfer.icns       macOS app icon (16-1024 px, macOS 11+ icon grid)
    open-transfer-1024.png   the same artwork as a PNG

The Windows icon is ``packaging/app_icon.ico`` (checked in). The macOS icon
follows Apple's grid: the rounded-square artwork fills 824 of 1024 pixels with
a soft shadow, so it sits well next to other apps in the Dock and Finder.
``scripts/build_app.py --desktop`` runs this before PyInstaller.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "open_transfer" / "static" / "icons" / "icon-512.png"
CANVAS = 1024
ARTWORK = 824  # Apple's macOS 11+ app icon grid
ICNS_SIZES = (32, 64, 128, 256, 512, 1024)


def master_icon(source: Path = SOURCE) -> Any:  # a PIL image; Pillow is only needed here
    from PIL import Image, ImageFilter

    art = Image.open(source).convert("RGBA").resize((ARTWORK, ARTWORK), Image.Resampling.LANCZOS)
    offset = (CANVAS - ARTWORK) // 2
    shadow_alpha = Image.new("L", (CANVAS, CANVAS), 0)
    shadow_alpha.paste(art.getchannel("A"), (offset, offset + 10))
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(14)).point(lambda a: a * 30 // 100)
    icon = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    icon.putalpha(shadow_alpha)
    icon.alpha_composite(art, (offset, offset))
    return icon


def make_icons(out: Path, source: Path = SOURCE) -> list[Path]:
    from PIL import Image

    out.mkdir(parents=True, exist_ok=True)
    icon = master_icon(source)
    png = out / "open-transfer-1024.png"
    icon.save(png)
    icns = out / "open-transfer.icns"
    sizes = [icon.resize((s, s), Image.Resampling.LANCZOS) for s in ICNS_SIZES]
    icon.save(icns, format="ICNS", append_images=sizes)
    return [icns, png]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "icons")
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    for path in make_icons(args.out, args.source):
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()

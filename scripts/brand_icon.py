"""Draw the Open Transfer app icon in code and write every icon asset from it.

    pip install playwright pillow && python -m playwright install chromium
    python scripts/brand_icon.py

The artwork ("Beam") is SVG on a 1024 grid: a continuous-corner squircle in a
cyan → blue → indigo gradient, faint discovery rings, and staggered send/receive
arrows. Chromium rasterises the SVG, so every PNG size is drawn from vectors
rather than downscaled. Set ``OT_CHROMIUM`` to use a specific Chromium binary.

Writes (relative to the repository root):

    src/open_transfer/static/icons/logo.svg                 web / PWA / README / site
    src/open_transfer/static/icons/icon-192.png, icon-512.png
    src/open_transfer/static/icons/icon-maskable-512.png    full bleed, arrows in the safe zone
    src/open_transfer/static/icons/apple-touch-icon.png     full bleed (iOS rounds it)
    src/open_transfer/static/favicon.ico                    16 / 32 / 48
    packaging/app_icon.ico                                  Windows, 16-256
    src/open_transfer/templates/index.html                  the inline ``#i-logo`` symbol
    android/.../drawable/ic_launcher_background.xml         adaptive icon layers
    android/.../drawable/ic_launcher_foreground.xml

The macOS ``.icns`` is built from ``icon-512.png`` by ``scripts/make_icons.py``.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "src" / "open_transfer" / "static" / "icons"
ANDROID_RES = ROOT / "android" / "app" / "src" / "main" / "res"
S = 1024
CENTER = S / 2

# Palette and geometry, shared by the SVG and the Android vectors.
GRADIENT = (("0", "#62D0FF"), (".42", "#1C7CFF"), (".78", "#2F4BEA"), ("1", "#3B2BCF"))
RINGS = ((232, 0.24, 9), (330, 0.15, 8), (428, 0.09, 7))  # radius, opacity, stroke
STROKE, HEAD, GAP, TOP, BOTTOM, STAGGER = 86, 104, 168, 296, 728, 48


def squircle(inset: float = 0.0, n: float = 5.0, steps: int = 256) -> str:
    """Superellipse |x|^n + |y|^n = 1: the continuous-corner shape of native app icons."""
    r = CENTER - inset
    pts = []
    for i in range(steps):
        a = 2 * math.pi * i / steps
        ca, sa = math.cos(a), math.sin(a)
        x = CENTER + r * math.copysign(abs(ca) ** (2 / n), ca)
        y = CENTER + r * math.copysign(abs(sa) ** (2 / n), sa)
        pts.append(f"{x:.1f} {y:.1f}")
    return "M" + "L".join(pts) + "Z"


def arrow_paths() -> tuple[str, str]:
    """The send (up, left) and receive (down, right) arrows as SVG path data."""
    lx, rx = CENTER - GAP, CENTER + GAP
    up_top, up_bot = TOP - STAGGER, BOTTOM - STAGGER
    dn_top, dn_bot = TOP + STAGGER, BOTTOM + STAGGER
    up = f"M{lx:g} {up_bot}V{up_top}M{lx - HEAD:g} {up_top + HEAD}L{lx:g} {up_top}L{lx + HEAD:g} {up_top + HEAD}"
    down = f"M{rx:g} {dn_top}V{dn_bot}M{rx - HEAD:g} {dn_bot - HEAD}L{rx:g} {dn_bot}L{rx + HEAD:g} {dn_bot - HEAD}"
    return up, down


def artwork(
    *, full_bleed: bool = False, glyph_scale: float = 1.0, p: str = "ot-", steps: int = 256
) -> str:
    """The icon as SVG markup on a 1024 grid (no outer <svg>). ``p`` prefixes element ids."""
    body = "M0 0H1024V1024H0Z" if full_bleed else squircle(steps=steps)
    stops = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in GRADIENT)
    rings = "".join(
        f'<circle cx="512" cy="512" r="{r}" fill="none" stroke="#fff" stroke-opacity="{o}" stroke-width="{w}"/>'
        for r, o, w in RINGS
    )
    up, down = arrow_paths()
    scale = (
        f' transform="translate(512 512) scale({glyph_scale:g}) translate(-512 -512)"'
        if glyph_scale != 1
        else ""
    )
    edge = (
        ""
        if full_bleed
        else f'<path d="{squircle(inset=4, steps=steps)}" fill="none" stroke="url(#{p}edge)" stroke-width="8"/>'
    )
    return f"""<defs>
<path id="{p}body" d="{body}"/>
<linearGradient id="{p}bg" x1=".1" y1="0" x2=".9" y2="1">{stops}</linearGradient>
<radialGradient id="{p}sheen" cx=".26" cy=".14" r=".8"><stop offset="0" stop-color="#fff" stop-opacity=".42"/><stop offset=".55" stop-color="#fff" stop-opacity="0"/></radialGradient>
<radialGradient id="{p}core" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#BFE6FF" stop-opacity=".38"/><stop offset="1" stop-color="#BFE6FF" stop-opacity="0"/></radialGradient>
<linearGradient id="{p}shade" x1="0" y1="0" x2="0" y2="1"><stop offset=".6" stop-color="#0A1260" stop-opacity="0"/><stop offset="1" stop-color="#0A1260" stop-opacity=".28"/></linearGradient>
<linearGradient id="{p}edge" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".55"/><stop offset=".35" stop-color="#fff" stop-opacity=".08"/><stop offset="1" stop-color="#fff" stop-opacity=".18"/></linearGradient>
<linearGradient id="{p}glyph" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#D6E7FF"/></linearGradient>
<filter id="{p}drop" x="-30%" y="-30%" width="160%" height="160%"><feDropShadow dx="0" dy="20" stdDeviation="24" flood-color="#0A136B" flood-opacity=".42"/></filter>
<clipPath id="{p}clip"><use href="#{p}body"/></clipPath>
</defs>
<use href="#{p}body" fill="url(#{p}bg)"/>
<g clip-path="url(#{p}clip)"><g{scale}>{rings}<circle cx="512" cy="512" r="300" fill="url(#{p}core)"/></g><use href="#{p}body" fill="url(#{p}sheen)"/><use href="#{p}body" fill="url(#{p}shade)"/></g>
<g filter="url(#{p}drop)"{scale}><g fill="none" stroke="url(#{p}glyph)" stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round"><path d="{up}"/><path d="{down}"/></g></g>
{edge}"""


def svg(**kw: Any) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">\n{artwork(**kw)}\n</svg>\n'


def sprite_symbol() -> str:
    """The ``#i-logo`` symbol for the app's inline sprite (64 grid, so existing sizing still works)."""
    inner = artwork(p="il-", steps=128).replace("\n", "")
    return (
        f'<symbol id="i-logo" viewBox="0 0 64 64"><g transform="scale(.0625)">{inner}</g></symbol>'
    )


# ---- Android adaptive icon (108dp, 66dp safe zone) ----
A = 108
A_SCALE = 0.072  # 1024-grid glyph -> 108dp, keeps the arrows inside the safe zone


def _a(v: float) -> str:
    return f"{A / 2 + (v - CENTER) * A_SCALE:.2f}".rstrip("0").rstrip(".")


def android_background() -> str:
    items = "\n".join(
        f'                <item android:offset="{float(o):g}" android:color="#FF{c[1:]}" />'
        for o, c in GRADIENT
    )
    rings = "\n".join(
        f'    <path android:pathData="M54,{54 - r * A_SCALE:.2f}a{r * A_SCALE:.2f},{r * A_SCALE:.2f} 0 1,1 0,{2 * r * A_SCALE:.2f}a{r * A_SCALE:.2f},{r * A_SCALE:.2f} 0 1,1 0,-{2 * r * A_SCALE:.2f}"\n'
        f'        android:strokeColor="#{round(o * 255):02X}FFFFFF" android:strokeWidth="{w * A_SCALE:.2f}" />'
        for r, o, w in RINGS
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by scripts/brand_icon.py: the icon's gradient and discovery rings. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:aapt="http://schemas.android.com/aapt"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path android:pathData="M0,0h108v108h-108z">
        <aapt:attr name="android:fillColor">
            <gradient android:type="linear" android:startX="10.8" android:startY="0" android:endX="97.2" android:endY="108">
{items}
            </gradient>
        </aapt:attr>
    </path>
{rings}
</vector>
"""


def android_foreground() -> str:
    lx, rx = CENTER - GAP, CENTER + GAP
    up_top, up_bot = TOP - STAGGER, BOTTOM - STAGGER
    dn_top, dn_bot = TOP + STAGGER, BOTTOM + STAGGER
    up = f"M{_a(lx)},{_a(up_bot)}V{_a(up_top)}M{_a(lx - HEAD)},{_a(up_top + HEAD)}L{_a(lx)},{_a(up_top)}L{_a(lx + HEAD)},{_a(up_top + HEAD)}"
    down = f"M{_a(rx)},{_a(dn_top)}V{_a(dn_bot)}M{_a(rx - HEAD)},{_a(dn_bot - HEAD)}L{_a(rx)},{_a(dn_bot)}L{_a(rx + HEAD)},{_a(dn_bot - HEAD)}"
    width = f"{STROKE * A_SCALE:.2f}"
    path = (
        '    <path\n        android:pathData="{d}"\n        android:strokeColor="#FFFFFF"\n'
        f'        android:strokeWidth="{width}"\n        android:strokeLineCap="round"\n        android:strokeLineJoin="round" />'
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by scripts/brand_icon.py: the send/receive arrows inside the 66dp safe zone.
     Also the monochrome (themed) icon and the loading-screen logo. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
{path.format(d=up)}
{path.format(d=down)}
</vector>
"""


# ---- rasterising ----
def render_png(markup: str, size: int, out: Path, page: Any) -> None:
    from PIL import Image

    sized = markup.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
    page.set_content(f'<html><body style="margin:0;background:transparent">{sized}</body></html>')
    page.screenshot(
        path=str(out), omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size}
    )
    Image.open(out).save(out, optimize=True)  # lossless recompression


def write_ico(frames: dict[int, Path], sizes: tuple[int, ...], out: Path) -> None:
    from PIL import Image

    images = [Image.open(frames[n]).convert("RGBA") for n in sizes]
    images[0].save(out, format="ICO", sizes=[(n, n) for n in sizes], append_images=images[1:])


def main() -> None:
    import tempfile

    from playwright.sync_api import sync_playwright

    icon, square = svg(), svg(full_bleed=True)
    maskable = svg(full_bleed=True, glyph_scale=0.78)
    (ICONS / "logo.svg").write_text(icon, encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("OT_CHROMIUM") or None)
        page = browser.new_page(viewport={"width": 1024, "height": 1024})
        frames = {}
        for n in (16, 24, 32, 48, 64, 128, 256):
            frames[n] = Path(tmp) / f"icon-{n}.png"
            render_png(icon, n, frames[n], page)
        render_png(icon, 192, ICONS / "icon-192.png", page)
        render_png(icon, 512, ICONS / "icon-512.png", page)
        render_png(maskable, 512, ICONS / "icon-maskable-512.png", page)
        render_png(square, 180, ICONS / "apple-touch-icon.png", page)
        browser.close()
        write_ico(frames, (48, 32, 16), ROOT / "src" / "open_transfer" / "static" / "favicon.ico")
        write_ico(frames, (256, 128, 64, 48, 32, 24, 16), ROOT / "packaging" / "app_icon.ico")

    template = ROOT / "src" / "open_transfer" / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")
    html, count = re.subn(
        r'<symbol id="i-logo".*?</symbol>', lambda _: sprite_symbol(), html, count=1, flags=re.S
    )
    if count != 1:
        raise SystemExit("index.html: #i-logo symbol not found")
    template.write_text(html, encoding="utf-8")

    (ANDROID_RES / "drawable" / "ic_launcher_background.xml").write_text(
        android_background(), encoding="utf-8"
    )
    (ANDROID_RES / "drawable" / "ic_launcher_foreground.xml").write_text(
        android_foreground(), encoding="utf-8"
    )
    print("icons written")


if __name__ == "__main__":
    main()

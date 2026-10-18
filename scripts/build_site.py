"""Assemble the project website into ``_site/`` (published to GitHub Pages).

    python scripts/build_site.py

Copies ``site/`` plus the logo, the README screenshots and the demo video, then checks that
every local ``src``/``href`` in the HTML points at a file that exists, so a
renamed screenshot fails CI instead of shipping a broken page.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_site"
ICONS = ROOT / "src" / "open_transfer" / "static" / "icons"


def build() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(ROOT / "site", OUT)
    shutil.copytree(ROOT / "docs" / "screenshots", OUT / "screenshots")
    shutil.copytree(ROOT / "docs" / "demo", OUT / "docs" / "demo")  # the README links the video here
    for name in ("logo.svg", "apple-touch-icon.png", "icon-512.png"):
        shutil.copy2(ICONS / name, OUT / name)
    (OUT / ".nojekyll").touch()  # serve files as-is, no Jekyll processing


def check_links() -> list[str]:
    missing = []
    for page in OUT.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href|srcset)="([^"#?]+)', html):
            if re.match(r"^(https?:|mailto:|data:|//)", ref) or ref in {"./", "/"}:
                continue
            if not (page.parent / ref).exists():
                missing.append(f"{page.relative_to(OUT)} → {ref}")
    return missing


def main() -> None:
    build()
    missing = check_links()
    if missing:
        sys.exit("Broken local links:\n  " + "\n  ".join(missing))
    files = sum(1 for p in OUT.rglob("*") if p.is_file())
    print(
        f"  Built {OUT.relative_to(ROOT)}/ ({files} files). Preview: python -m http.server -d _site"
    )


if __name__ == "__main__":
    main()

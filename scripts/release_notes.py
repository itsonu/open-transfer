"""Print the CHANGELOG.md section for a version, as the GitHub release notes.

    python scripts/release_notes.py v3.0.0 > notes.md

Fails if the section is missing or still says "unreleased" instead of a date,
so a release can't go out with the wrong notes or an undated changelog.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(changelog: str, version: str) -> str:
    version = version.removeprefix("v")
    heading = re.search(rf"^## \[{re.escape(version)}\](.*)$", changelog, re.MULTILINE)
    if not heading:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    if not re.search(r"\d{4}-\d{2}-\d{2}", heading[1]):
        raise ValueError(f"CHANGELOG.md section {version} has no release date: {heading[0]!r}")
    rest = changelog[heading.end() :]
    end = re.search(r"^(## |\[[^\]]+\]: )", rest, re.MULTILINE)
    return rest[: end.start() if end else None].strip() + "\n"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: release_notes.py vX.Y.Z")
    try:
        print(section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1]), end="")
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()

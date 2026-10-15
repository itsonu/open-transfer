"""scripts/release_notes.py — the CHANGELOG section a release publishes."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

CHANGELOG = """# Changelog

## [Unreleased]

## [1.1.0] — unreleased

### Added
- Next thing.

## [1.0.0] — 2026-01-02

### Added
- First thing.

[1.0.0]: https://example.com/v1.0.0
"""


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_notes", ROOT / "scripts/release_notes.py"
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_returns_only_that_version_section() -> None:
    assert load_script().section(CHANGELOG, "v1.0.0") == "### Added\n- First thing.\n"


def test_undated_section_is_refused() -> None:
    with pytest.raises(ValueError, match="no release date"):
        load_script().section(CHANGELOG, "v1.1.0")


def test_missing_section_is_refused() -> None:
    with pytest.raises(ValueError, match=r"no section for 9\.9\.9"):
        load_script().section(CHANGELOG, "v9.9.9")


def test_real_changelog_has_the_package_version() -> None:
    from open_transfer import __version__

    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text

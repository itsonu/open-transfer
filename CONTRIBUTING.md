# Contributing to Open Transfer

Thanks for helping! Bug reports, docs fixes, design polish and code are all welcome.

## Ways to help

- **Report a bug** — use the [bug template](https://github.com/itsonu/open-transfer/issues/new/choose). Include the sharing OS, the device/browser that failed and `--verbose` output.
- **Suggest a feature** — describe the problem first; check the [roadmap](README.md#roadmap).
- **Pick up an issue** — look for `good first issue` and `help wanted`. Comment so nobody duplicates work.
- **Security issues** — please don't open a public issue; see [SECURITY.md](SECURITY.md).

## Set up (≈2 minutes)

Requirements: Python 3.10+, Git, and optionally Node.js (for `node --check` in `make lint`).

```bash
git clone https://github.com/itsonu/open-transfer.git
cd open-transfer
make setup        # .venv with dev tools, pre-commit hooks, Playwright Chromium
make dev          # runs on ./.dev-share with request logging
```

No `make`? The equivalents are:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,e2e]"
pre-commit install
python -m playwright install chromium
open-transfer .dev-share --verbose
```

## Before you open a pull request

```bash
make check        # ruff lint + format check, mypy, unit tests, browser tests
```

- **Keep PRs focused** — one change per PR is easier to review and revert.
- **Add or update tests.** API changes → `tests/test_api.py`; storage → `tests/test_storage.py`; UI flows → `tests/e2e/test_ui.py`.
- **UI changes:** check light and dark mode, a phone-sized viewport, keyboard-only use, and *Reduce Motion*. Regenerate screenshots with `python scripts/screenshots.py` if the look changed, and include before/after images in the PR.
- **Docs:** update `README.md` (options table), `docs/api.md` (endpoints) and add a line to `CHANGELOG.md` under *Unreleased*.
- **Commits:** clear, imperative messages (`Add folder uploads`, `Fix ETA for tiny files`). [Conventional Commits](https://www.conventionalcommits.org/) prefixes are welcome but not required.

## Code style

- Python is formatted and linted with **Ruff** (`make fmt`) and type-checked with **mypy --strict**. Pre-commit runs Ruff on every commit.
- Front end: plain HTML/CSS and one ES module — **no build step, no framework, no external requests**. Match the existing sections and naming; use the CSS custom properties in `:root` rather than new hard-coded colours.
- Prefer small, well-named functions and comments that explain *why*, not *what*.
- New user-facing text: short, friendly, specific ("Couldn’t send the file — connection lost"), no jargon.

See [docs/architecture.md](docs/architecture.md) for how the pieces fit together and where new code should go.

## Releasing (maintainers)

1. Update `__version__` in `src/open_transfer/__init__.py` and move *Unreleased* notes in `CHANGELOG.md` under the new version, dated (`## [X.Y.Z] — YYYY-MM-DD`). That section becomes the GitHub release notes; the release fails without it.
2. Tag and push: `git tag v2.1.0 && git push --tags` — or, without pushing a tag, run **Actions → Release → Run workflow** on `master` with tag `v2.1.0` (the workflow creates the tag). The tag must match `__version__`.
3. The *Release* workflow builds the wheel/sdist, the standalone app for Linux, macOS and Windows (via `scripts/build_app.py`), a multi-arch Docker image on GHCR, and a GitHub release.

To build the standalone app locally: `python3 scripts/build_app.py` (or `make app`).

### Website

The project site lives in `site/` and is published to https://itsonu.github.io/open-transfer/ by the *Website* workflow on every push to `master` that touches it. Preview locally with `python scripts/build_site.py && python -m http.server -d _site`. One-time repository setting: **Settings → Pages → Build and deployment → Source: GitHub Actions**.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE) and to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

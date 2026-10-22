"""Opening or revealing a received file on the device itself (owner only), and
inline PDF previews."""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from open_transfer import opener
from tests.conftest import AppFactory, upload

VISITOR = {"REMOTE_ADDR": "192.168.1.50"}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(opener, "available", lambda: True)
    monkeypatch.setattr(opener, "open_file", lambda path: seen.append(("open", path)))
    monkeypatch.setattr(opener, "reveal", lambda path: seen.append(("reveal", path)))
    return seen


@pytest.fixture
def owner(make_app: AppFactory) -> FlaskClient:
    return make_app(owner_loopback=True, share_folder=False).test_client()


def put(client: FlaskClient, root: Path, name: str, data: bytes = b"hi") -> Path:
    path = root / name
    path.write_bytes(data)
    return path


def root_of(client: FlaskClient) -> Path:
    return Path(client.application.extensions["open_transfer"]["config"].storage_dir)


def test_owner_can_open_and_reveal_a_received_file(
    owner: FlaskClient, calls: list[tuple[str, Path]]
) -> None:
    path = put(owner, root_of(owner), "notes.txt")
    assert owner.post("/api/files/notes.txt/open").status_code == 200
    assert owner.post("/api/files/notes.txt/reveal").status_code == 200
    assert calls == [("open", path.resolve()), ("reveal", path.resolve())]


@pytest.mark.parametrize(
    "name", ["setup.exe", "run.sh", "Tool.app.zip.command", "x.desktop", "a.bat"]
)
def test_programs_are_revealed_never_opened(
    owner: FlaskClient, calls: list[tuple[str, Path]], name: str
) -> None:
    put(owner, root_of(owner), name)
    res = owner.post(f"/api/files/{name}/open")
    assert res.status_code == 400
    assert res.json["error"]["code"] == "unsafe_to_open"
    assert owner.post(f"/api/files/{name}/reveal").status_code == 200
    assert [kind for kind, _ in calls] == ["reveal"]


def test_visitors_cannot_open_files_on_the_device(
    make_app: AppFactory, calls: list[tuple[str, Path]]
) -> None:
    client = make_app(owner_loopback=True).test_client()  # shared folder, browsing allowed
    upload(client, "notes.txt", b"hi")
    for action in ("open", "reveal"):
        res = client.post(f"/api/files/notes.txt/{action}", environ_base=VISITOR)
        assert res.status_code == 403
    assert calls == []


def test_missing_and_escaping_names(owner: FlaskClient, calls: list[tuple[str, Path]]) -> None:
    assert owner.post("/api/files/nope.txt/reveal").status_code == 404
    assert owner.post("/api/files/..%2Fsecret.txt/reveal").status_code in {400, 404}
    assert calls == []


def test_cross_site_requests_are_refused(owner: FlaskClient, calls: list[tuple[str, Path]]) -> None:
    put(owner, root_of(owner), "notes.txt")
    res = owner.post(
        "/api/files/notes.txt/open",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert res.status_code == 403
    assert calls == []


def test_no_opener_on_this_machine(owner: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opener, "available", lambda: False)
    put(owner, root_of(owner), "notes.txt")
    assert owner.get("/api/info").json["local_open"] is False
    res = owner.post("/api/files/notes.txt/reveal")
    assert res.status_code == 501
    assert res.json["error"]["code"] == "no_opener"


def test_info_says_when_files_can_be_opened_here(owner: FlaskClient, calls: object) -> None:
    assert owner.get("/api/info").json["local_open"] is True


def test_visitors_never_see_local_open(make_app: AppFactory, calls: object) -> None:
    client = make_app(owner_loopback=True).test_client()
    assert client.get("/api/info", environ_base=VISITOR).json["local_open"] is False


# ------------------------------------------------------------ opener commands


FILE = Path("/f/a b.txt")  # written with this OS's separators by str()


@pytest.mark.parametrize(
    ("platform", "action", "expected"),
    [
        ("darwin", "open", ["open", str(FILE)]),
        ("darwin", "reveal", ["open", "-R", str(FILE)]),
        ("linux", "open", ["xdg-open", str(FILE)]),
        ("linux", "reveal", ["xdg-open", str(FILE.parent)]),
        ("win32", "reveal", ["explorer", "/select,", str(FILE)]),
    ],
)
def test_opener_commands(platform: str, action: str, expected: list[str]) -> None:
    assert opener.command(action, FILE, platform) == expected


def test_unsafe_names() -> None:
    for name in ("a.EXE", "b.msi", "c.ps1", "d.vbs", "e.jar", "f.apk", "g.scr", "h.lnk", "i.sh"):
        assert opener.unsafe_to_open(name), name
    for name in ("photo.jpg", "report.pdf", "notes.txt", "song.mp3", "archive.zip"):
        assert not opener.unsafe_to_open(name), name


# ------------------------------------------------------------- inline PDFs


def test_pdfs_can_be_previewed_inside_the_app(client: FlaskClient) -> None:
    upload(client, "doc.pdf", b"%PDF-1.4 ...")
    res = client.get("/files/doc.pdf?inline=1")
    assert res.headers["Content-Disposition"].startswith("inline")
    assert res.mimetype == "application/pdf"
    csp = res.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in csp
    assert res.headers["X-Frame-Options"] == "SAMEORIGIN"
    # Downloads and other files can still never be framed.
    other = client.get("/files/doc.pdf")
    assert other.headers["Content-Disposition"].startswith("attachment")
    assert other.headers["X-Frame-Options"] == "DENY"
    upload(client, "pic.png", b"\x89PNG....")
    assert "sandbox" in client.get("/files/pic.png?inline=1").headers["Content-Security-Policy"]

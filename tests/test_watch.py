"""Watch in the browser: the owner shares a video from the folder, visitors stream it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from open_transfer import picker
from tests.conftest import AppFactory

VISITOR = {"REMOTE_ADDR": "192.168.1.20"}


def setup(make_app: AppFactory, **config: object) -> tuple[FlaskClient, FlaskClient, Path]:
    app = make_app(share_folder=False, owner_loopback=True, **config)
    root = Path(app.extensions["open_transfer"]["config"].storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    visitor = app.test_client()
    visitor.environ_base.update(VISITOR)
    return app.test_client(), visitor, root


def test_visitors_stream_only_what_the_owner_shares(make_app: AppFactory) -> None:
    owner, visitor, root = setup(make_app)
    (root / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    (root / "private.mp4").write_bytes(b"p" * 100)
    (root / "notes.txt").write_text("hi")

    assert visitor.get("/api/watch").json == {"files": []}
    assert visitor.post("/api/watch", json={"name": "clip.mp4"}).status_code == 403
    assert owner.post("/api/watch", json={"name": "notes.txt"}).status_code == 400
    assert owner.post("/api/watch", json={"name": "../etc/passwd"}).status_code == 404

    shared = owner.post("/api/watch", json={"name": "clip.mp4"})
    assert shared.status_code == 201
    token = shared.json["id"]
    assert owner.post("/api/watch", json={"name": "clip.mp4"}).json["id"] == token  # same share

    (listed,) = visitor.get("/api/watch").json["files"]
    assert (listed["name"], listed["size"], listed["url"]) == (
        "clip.mp4",
        1024,
        f"/api/watch/{token}",
    )

    part = visitor.get(listed["url"], headers={"Range": "bytes=300-309"})
    assert part.status_code == 206
    assert part.data == (bytes(range(256)) * 4)[300:310]
    assert part.headers["Content-Range"] == "bytes 300-309/1024"

    # Nothing else: not the folder, not a guessed id.
    assert visitor.get("/files/private.mp4").status_code == 403
    assert visitor.get("/api/watch/not-a-share").status_code == 404
    assert visitor.delete(f"/api/watch/{token}").status_code == 403

    assert owner.delete(f"/api/watch/{token}").status_code == 200
    assert visitor.get(listed["url"]).status_code == 404
    assert visitor.get("/api/watch").json == {"files": []}


def test_a_shared_video_that_is_gone_is_not_listed(make_app: AppFactory) -> None:
    owner, visitor, root = setup(make_app)
    (root / "clip.webm").write_bytes(b"x" * 10)
    url = f"/api/watch/{owner.post('/api/watch', json={'name': 'clip.webm'}).json['id']}"
    (root / "clip.webm").unlink()
    assert visitor.get("/api/watch").json == {"files": []}
    assert visitor.get(url).status_code == 404


def test_mkv_plays_inline_and_seeks_past_4_gb(make_app: AppFactory) -> None:
    owner, visitor, root = setup(make_app)
    big = root / "film.mkv"
    with big.open("wb") as fh:  # sparse: no real disk used
        fh.truncate(5 * 1024**3)
        fh.seek(5_000_000_000)
        fh.write(b"0123456789")
    url = f"/api/watch/{owner.post('/api/watch', json={'name': 'film.mkv'}).json['id']}"

    part = visitor.get(url, headers={"Range": "bytes=5000000000-5000000009"})
    assert part.status_code == 206
    assert part.data == b"0123456789"
    assert part.headers["Content-Range"] == f"bytes 5000000000-5000000009/{5 * 1024**3}"
    assert part.headers["Content-Type"] == "video/x-matroska"
    assert part.headers["Content-Disposition"].startswith("inline")


def test_watching_needs_the_pin(make_app: AppFactory) -> None:
    owner, visitor, root = setup(make_app, pin="4821")
    (root / "clip.mp4").write_bytes(b"x" * 10)
    url = f"/api/watch/{owner.post('/api/watch', json={'name': 'clip.mp4'}).json['id']}"
    assert visitor.get(url).status_code == 401
    assert visitor.get("/api/watch").status_code == 401
    visitor.post("/api/auth", json={"pin": "4821"})
    assert visitor.get(url).status_code == 200


def test_a_video_from_anywhere_waits_unshared_until_the_owner_shares_it(
    make_app: AppFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, visitor, _ = setup(make_app)
    elsewhere = tmp_path / "Movies"
    elsewhere.mkdir()
    film = elsewhere / "film.mkv"
    film.write_bytes(bytes(range(200)))
    chosen: list[Path | None] = [film, None, elsewhere / "notes.txt"]
    (elsewhere / "notes.txt").write_text("hi")
    monkeypatch.setattr(picker, "available", lambda *_: True)
    monkeypatch.setattr(picker, "pick_video", lambda *_: chosen.pop(0))

    assert visitor.post("/api/watch/pick").status_code == 403
    picked = owner.post("/api/watch/pick")
    assert picked.status_code == 201
    token = picked.json["id"]
    assert (picked.json["name"], picked.json["size"]) == ("film.mkv", 200)

    # Previewing is for the owner alone: no visitor sees or streams it yet.
    assert visitor.get("/api/watch").json == {"files": []}
    assert visitor.get(f"/api/watch/{token}").status_code == 404
    assert (
        owner.get(f"/api/watch/{token}", headers={"Range": "bytes=10-19"}).data
        == film.read_bytes()[10:20]
    )
    assert owner.get("/api/watch").json == {"files": []}
    assert [f["id"] for f in owner.get("/api/watch?all=1").json["files"]] == [token]
    assert visitor.get("/api/watch?all=1").json == {"files": []}  # only the owner sees the unshared

    assert visitor.post(f"/api/watch/{token}/share").status_code == 403
    assert owner.post(f"/api/watch/{token}/share").status_code == 200
    (listed,) = visitor.get("/api/watch").json["files"]
    assert listed["name"] == "film.mkv"
    assert visitor.get(listed["url"], headers={"Range": "bytes=0-4"}).status_code == 206

    assert owner.post("/api/watch/pick").json == {"canceled": True}
    assert owner.post("/api/watch/pick").status_code == 400  # a text file

    assert owner.delete(f"/api/watch/{token}").status_code == 200
    assert visitor.get(f"/api/watch/{token}").status_code == 404

    # A video that went away is no longer offered.
    film.unlink()
    assert owner.get("/api/watch?all=1").json == {"files": []}


def test_picker_commands_name_the_systems_own_dialog() -> None:
    assert picker.command("darwin")[0] == "osascript"
    assert picker.command("win32")[0] == "powershell"
    assert picker.available("darwin")
    assert picker.available("win32")


def test_the_picker_reads_a_name_that_isnt_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess([], 0, stdout="\ufeffC:\\Видео\\फिल्म.mp4\r\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    assert picker.pick_video("win32") == Path("C:\\Видео\\फिल्म.mp4")
    done.returncode = 1  # the dialog was closed
    assert picker.pick_video("win32") is None

"""Watch in the browser: the owner shares a video from the folder, visitors stream it."""

from __future__ import annotations

from pathlib import Path

from flask.testing import FlaskClient

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

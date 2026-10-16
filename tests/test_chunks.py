"""Resumable chunked uploads: big files split into Content-Range pieces."""

from __future__ import annotations

from urllib.parse import quote

from flask.testing import FlaskClient

from open_transfer.chunks import CHUNK_SIZE, parse_content_range


def test_parse_content_range() -> None:
    assert parse_content_range("bytes 0-99/1234") == (0, 99, 1234)
    assert parse_content_range("bytes 2097152-4194303/5242880") == (2097152, 4194303, 5242880)
    assert parse_content_range(None) is None
    assert parse_content_range("bytes */1234") is None
    assert parse_content_range("bytes 5-3/10") is None
    assert parse_content_range("bytes 0-10/5") is None
    assert parse_content_range("garbage") is None


def _put(client: FlaskClient, start: int, end: int, total: int, uid: str, data: bytes):
    return client.post(
        "/api/files",
        data=data,
        headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
            "X-Upload-Id": uid,
            "X-Filename": quote("big.bin"),
            "Content-Type": "application/octet-stream",
        },
    )


def test_chunked_upload_assembles_file(client: FlaskClient) -> None:
    total = 5 * 1024 * 1024
    payload = b"A" * total
    uid = "chunk-test-123"
    first = _put(client, 0, CHUNK_SIZE - 1, total, uid, payload[:CHUNK_SIZE])
    assert first.status_code == 308
    assert first.json["received"] == CHUNK_SIZE
    assert first.headers["Range"] == f"bytes=0-{CHUNK_SIZE - 1}"

    second = _put(
        client, CHUNK_SIZE, 2 * CHUNK_SIZE - 1, total, uid, payload[CHUNK_SIZE : 2 * CHUNK_SIZE]
    )
    assert second.status_code == 308

    rest = payload[2 * CHUNK_SIZE :]
    last = _put(client, 2 * CHUNK_SIZE, total - 1, total, uid, rest)
    assert last.status_code == 201
    assert last.json["files"][0]["size"] == total
    assert client.get("/files/big.bin").data == payload


def test_chunked_upload_out_of_order_is_accepted(client: FlaskClient) -> None:
    # Parallel chunks may arrive in any order; every valid offset is stored.
    total = 100
    uid = "chunk-test-offset"
    first = _put(client, 0, 9, total, uid, b"0123456789")
    assert first.status_code == 308
    ahead = _put(client, 50, 59, total, uid, b"0123456789")
    assert ahead.status_code == 308
    assert ahead.json["received"] == 20
    assert client.get(f"/api/uploads/{uid}/status").json["ranges"] == [[0, 10], [50, 60]]


def test_chunked_upload_rewrite_is_idempotent(client: FlaskClient) -> None:
    total = 100
    uid = "chunk-test-rewrite"
    assert _put(client, 0, 9, total, uid, b"0123456789").status_code == 308
    again = _put(client, 0, 9, total, uid, b"0123456789")
    assert again.status_code == 308
    assert again.json["received"] == 10


def test_chunked_upload_bad_range_is_rejected(client: FlaskClient) -> None:
    res = client.post(
        "/api/files",
        data=b"x",
        headers={
            "Content-Range": "bytes nonsense",
            "X-Upload-Id": "bad-range-uid",
            "X-Filename": "a.bin",
            "Content-Type": "application/octet-stream",
        },
    )
    assert res.status_code == 400
    assert res.json["error"]["code"] == "bad_range"


def test_upload_status_endpoint(client: FlaskClient) -> None:
    total = 100
    uid = "chunk-test-status"
    assert client.get(f"/api/uploads/{uid}/status").json == {
        "received": 0,
        "total": 0,
        "ranges": [],
    }
    _put(client, 0, 9, total, uid, b"0123456789")
    status = client.get(f"/api/uploads/{uid}/status").json
    assert status["received"] == 10
    assert status["total"] == 100
    assert status["ranges"] == [[0, 10]]


def test_invite_link_is_single_use(make_app) -> None:
    app = make_app(pin="1234")
    app.config["TESTING"] = True
    # Minting needs the owner (loopback), which make_app disables for HTTP,
    # so mint through the mesh — the HTTP redeem path below is what matters.
    mesh = app.extensions["open_transfer"]["mesh"]
    invite = mesh.create_invite(ttl=600, single_use=True)
    assert invite["single_use"] is True
    # First visitor redeems it past the PIN.
    guest = app.test_client()
    assert guest.get(f"/?invite={invite['token']}", follow_redirects=False).status_code == 302
    assert guest.get("/api/info").json["auth"]["authenticated"] is True
    # Second visitor gets nothing: the token is spent.
    stranger = app.test_client()
    assert stranger.get(f"/?invite={invite['token']}", follow_redirects=False).status_code == 302
    assert stranger.get("/api/info").json["auth"]["authenticated"] is False

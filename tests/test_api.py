from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from urllib.parse import quote

from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import EnvironBuilder, run_wsgi_app

from tests.conftest import AppFactory, upload


def test_health(client: FlaskClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json["status"] == "ok"


def test_index_renders_with_boot_info(client: FlaskClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Open Transfer" in html
    assert 'id="boot"' in html
    assert "Content-Security-Policy" in res.headers


def test_legacy_pages_redirect(client: FlaskClient) -> None:
    for path in ["/upload", "/downloads"]:
        res = client.get(path)
        assert res.status_code == 301
        assert res.headers["Location"].endswith("/")


def test_manifest(client: FlaskClient) -> None:
    for path in ["/manifest.webmanifest", "/manifest.json"]:
        res = client.get(path)
        assert res.status_code == 200
        assert res.json["name"] == "Open Transfer"


def test_upload_list_download_roundtrip(client: FlaskClient, share_dir: Path) -> None:
    res = upload(client, "Café report.pdf", b"%PDF-1.7 data")
    assert res.status_code == 201
    saved = res.json["files"][0]
    assert saved["name"] == "Café report.pdf"
    assert saved["size"] == 13
    assert saved["kind"] == "document"

    listing = client.get("/api/files")
    assert [f["name"] for f in listing.json["files"]] == ["Café report.pdf"]
    assert listing.json["total_size"] == 13

    res = client.get("/files/Caf%C3%A9%20report.pdf")
    assert res.status_code == 200
    assert res.data == b"%PDF-1.7 data"
    assert "attachment" in res.headers["Content-Disposition"]
    assert "sandbox" in res.headers["Content-Security-Policy"]


def test_legacy_download_route(client: FlaskClient) -> None:
    upload(client, "a.txt", b"abc")
    assert client.get("/download/a.txt").data == b"abc"


def test_range_requests_for_resumable_downloads(client: FlaskClient) -> None:
    upload(client, "a.bin", b"0123456789")
    res = client.get("/files/a.bin", headers={"Range": "bytes=2-5"})
    assert res.status_code == 206
    assert res.data == b"2345"


def test_list_supports_etag(client: FlaskClient) -> None:
    first = client.get("/api/files")
    etag = first.headers["ETag"]
    again = client.get("/api/files", headers={"If-None-Match": etag})
    assert again.status_code == 304
    upload(client, "a.txt")
    assert client.get("/api/files", headers={"If-None-Match": etag}).status_code == 200


def test_multipart_upload_legacy_endpoint(client: FlaskClient) -> None:
    res = client.post(
        "/transfer",
        data={"file": [(io.BytesIO(b"one"), "one.txt"), (io.BytesIO(b"two"), "two.txt")]},
        content_type="multipart/form-data",
    )
    assert res.status_code == 201
    assert sorted(f["name"] for f in res.json["files"]) == ["one.txt", "two.txt"]


def test_multipart_without_file(client: FlaskClient) -> None:
    res = client.post("/api/files", data={}, content_type="multipart/form-data")
    assert res.status_code == 400
    assert res.json["error"]["code"] == "no_file"


def test_upload_requires_name(client: FlaskClient) -> None:
    res = client.post("/api/files", data=b"x", content_type="application/octet-stream")
    assert res.status_code == 400
    assert res.json["error"]["code"] == "invalid_name"


def test_upload_name_is_sanitised(client: FlaskClient, share_dir: Path) -> None:
    res = upload(client, "../../evil.sh")
    assert res.json["files"][0]["name"] == "evil.sh"
    assert (share_dir / "evil.sh").exists()
    assert not (share_dir.parent / "evil.sh").exists()


def test_upload_size_limit(make_app: AppFactory) -> None:
    client = make_app(max_upload_size=4).test_client()
    res = upload(client, "big.bin", b"12345")
    assert res.status_code == 413
    assert res.json["error"]["code"] == "too_large"
    assert client.get("/api/files").json["files"] == []


def test_duplicate_upload_is_renamed(client: FlaskClient) -> None:
    upload(client, "a.txt")
    assert upload(client, "a.txt").json["files"][0]["name"] == "a (1).txt"


def test_download_missing_and_traversal(client: FlaskClient) -> None:
    assert client.get("/files/nope.txt").status_code == 404
    assert client.get("/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/files/.open-transfer/secret").status_code == 404


def test_html_error_page_for_browsers(client: FlaskClient) -> None:
    res = client.get("/files/nope.txt", headers={"Accept": "text/html"})
    assert res.status_code == 404
    assert "text/html" in res.headers["Content-Type"]
    assert "Back to Open Transfer" in res.get_data(as_text=True)


def test_inline_preview_only_for_safe_types(client: FlaskClient) -> None:
    upload(client, "pic.png", b"\x89PNG....")
    upload(client, "page.html", b"<script>alert(1)</script>")
    assert "inline" in client.get("/files/pic.png?inline=1").headers["Content-Disposition"]
    html = client.get("/files/page.html?inline=1")
    assert "attachment" in html.headers["Content-Disposition"]


def test_delete_and_undo(client: FlaskClient) -> None:
    upload(client, "a.txt", b"abc")
    res = client.delete("/api/files/a.txt")
    assert res.status_code == 200
    token = res.json["undo_token"]
    assert client.get("/api/files").json["files"] == []
    restored = client.post(f"/api/trash/{token}/restore")
    assert restored.status_code == 200
    assert restored.json["file"]["name"] == "a.txt"
    assert client.get("/files/a.txt").data == b"abc"


def test_delete_missing(client: FlaskClient) -> None:
    res = client.delete("/api/files/nope.txt")
    assert res.status_code == 404


def test_archive_all_and_selected(client: FlaskClient) -> None:
    upload(client, "a.txt", b"A" * 1000)
    upload(client, "b.txt", b"B")
    res = client.get("/api/archive")
    assert res.status_code == 200
    assert res.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(res.data)) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "b.txt"]
        assert zf.read("a.txt") == b"A" * 1000
        assert zf.testzip() is None
    res = client.get("/api/archive?name=b.txt")
    with zipfile.ZipFile(io.BytesIO(res.data)) as zf:
        assert zf.namelist() == ["b.txt"]


def test_archive_empty(client: FlaskClient) -> None:
    assert client.get("/api/archive").status_code == 404


def test_qr_code_svg(client: FlaskClient) -> None:
    res = client.get("/api/qr.svg")
    assert res.status_code == 200
    assert res.mimetype == "image/svg+xml"
    assert b"<svg" in res.data


def test_info_reports_permissions(make_app: AppFactory) -> None:
    info = make_app(allow_delete=False, max_upload_size=100).test_client().get("/api/info").json
    assert info["permissions"] == {"upload": True, "browse": True, "delete": False, "send": True}
    assert info["limits"]["max_upload_size"] == 100
    assert info["share_url"].startswith("http://")


def test_public_url_is_advertised(make_app: AppFactory) -> None:
    info = make_app(public_url="https://files.example.com/").test_client().get("/api/info").json
    assert info["share_url"] == "https://files.example.com"
    assert info["urls"] == ["https://files.example.com"]


def test_read_only_mode(make_app: AppFactory) -> None:
    client = make_app(allow_upload=False, allow_delete=False).test_client()
    assert upload(client, "a.txt").status_code == 403
    assert client.get("/api/files").status_code == 200


def test_no_delete_mode(make_app: AppFactory) -> None:
    client = make_app(allow_delete=False).test_client()
    upload(client, "a.txt")
    res = client.delete("/api/files/a.txt")
    assert res.status_code == 403
    assert res.json["error"]["code"] == "delete_disabled"


def test_receive_only_mode(make_app: AppFactory) -> None:
    client = make_app(allow_browse=False).test_client()
    assert upload(client, "a.txt").status_code == 201
    assert client.get("/api/files").status_code == 403
    assert client.get("/files/a.txt").status_code == 403
    assert client.get("/api/archive").status_code == 403
    deleted = client.delete("/api/files/a.txt")
    assert deleted.status_code == 403
    assert client.get("/api/info").json["permissions"]["browse"] is False


def test_share_url_from_host_header(client: FlaskClient) -> None:
    assert client.get("/api/info", headers={"Host": "192.168.1.5"}).json["share_url"] == (
        "http://192.168.1.5"
    )
    info = client.get("/api/info", headers={"Host": "192.168.1.5:5000"}).json
    assert info["share_url"] == "http://192.168.1.5:5000"
    assert all(u.endswith(":5000") for u in info["urls"])


def test_upload_without_length_on_non_streaming_server(app: Flask, client: FlaskClient) -> None:
    # e.g. a chunked upload through a WSGI server that doesn't de-chunk bodies
    environ = EnvironBuilder(
        path="/api/files", method="POST", headers={"X-Filename": "a.txt"}, data=b"x"
    ).get_environ()
    environ.pop("CONTENT_LENGTH")
    environ["wsgi.input_terminated"] = False
    _, status, _ = run_wsgi_app(app, environ, buffered=True)
    assert status.startswith("411")
    assert client.get("/api/files").json["files"] == []


def test_files_copied_in_by_the_owner_are_downloadable(
    client: FlaskClient, share_dir: Path
) -> None:
    # Names safe_filename would rewrite: macOS NFD, "?", double spaces, 244-byte dedupes
    names = ["Cafe\u0301.pdf", "two  spaces.txt"]
    if os.name != "nt":  # Windows can't create names containing "?"
        names.append("what?.txt")
    for name in names:
        (share_dir / name).write_bytes(b"x")
    long_name = "é" * 118 + ".txt"  # 240 bytes but only 122 characters (Windows MAX_PATH)
    upload(client, long_name)
    second = upload(client, long_name).json["files"][0]["name"]
    listed = {f["name"] for f in client.get("/api/files").json["files"]}
    for name in [*names, second]:
        assert name in listed
        assert client.get(f"/files/{quote(name)}").status_code == 200, name
    with zipfile.ZipFile(io.BytesIO(client.get("/api/archive").data)) as zf:
        assert len(zf.namelist()) == len(names) + 2
    res = client.delete(f"/api/files/{quote(second)}")
    assert res.status_code == 200


def test_archive_handles_files_older_than_1980(client: FlaskClient, share_dir: Path) -> None:
    upload(client, "old.txt", b"vintage")
    os.utime(share_dir / "old.txt", (0, 0))
    with zipfile.ZipFile(io.BytesIO(client.get("/api/archive").data)) as zf:
        assert zf.read("old.txt") == b"vintage"


def test_share_url_uses_the_port_the_visitor_used(app: Flask) -> None:
    app.config["OT_PORT"] = 5000  # listen port inside a container
    client = app.test_client()
    info = client.get("/api/info", headers={"Host": "192.168.1.5:8080"}).json
    assert info["share_url"] == "http://192.168.1.5:8080"


def test_name_query_param_is_not_decoded_twice(client: FlaskClient) -> None:
    res = client.post(
        "/api/files?name=100%2525.txt", data=b"x", content_type="application/octet-stream"
    )
    assert res.json["files"][0]["name"] == "100%25.txt"

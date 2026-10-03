from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from open_transfer.security import RateLimiter, host_allowed
from tests.conftest import AppFactory, upload


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("192.168.1.20:5000", True),
        ("10.0.0.5", True),
        ("[::1]:5000", True),
        ("localhost:5000", True),
        ("my-laptop.local:5000", True),
        ("nas.home.arpa", True),
        ("evil.example.com", False),
        ("evil.example.com:5000", False),
        ("", False),
    ],
)
def test_host_allowed(host: str, allowed: bool) -> None:
    assert host_allowed(host, (), ("my-laptop",)) is allowed


def test_host_allowed_explicit_and_wildcard() -> None:
    assert host_allowed("files.example.com", ("files.example.com",), ())
    assert host_allowed("anything.example", ("*",), ())
    assert host_allowed("my-laptop:5000", (), ("my-laptop",))


def test_dns_rebinding_blocked(client: FlaskClient) -> None:
    res = client.get("/api/files", headers={"Host": "attacker.example"})
    assert res.status_code == 421
    assert res.json["error"]["code"] == "host_not_allowed"


def test_allowed_host_option(make_app: AppFactory) -> None:
    client = make_app(allowed_hosts=("files.example.com",)).test_client()
    assert client.get("/api/files", headers={"Host": "files.example.com"}).status_code == 200


def test_cross_site_writes_rejected(client: FlaskClient) -> None:
    res = upload(client, "a.txt", Origin="http://evil.example")
    assert res.status_code == 403
    res = upload(client, "a.txt", **{"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403
    res = upload(client, "a.txt", Origin="null")
    assert res.status_code == 403


def test_same_origin_and_non_browser_writes_allowed(client: FlaskClient) -> None:
    assert upload(client, "a.txt", Origin="http://localhost").status_code == 201
    assert upload(client, "b.txt", **{"Sec-Fetch-Site": "same-origin"}).status_code == 201
    assert upload(client, "c.txt").status_code == 201  # curl sends neither header


def test_security_headers(client: FlaskClient) -> None:
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in res.headers["Content-Security-Policy"]
    assert client.get("/api/files").headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ PIN auth


@pytest.fixture
def locked(make_app: AppFactory) -> FlaskClient:
    return make_app(pin="4821").test_client()


def test_pin_blocks_everything_but_shell(locked: FlaskClient) -> None:
    assert locked.get("/").status_code == 200
    assert locked.get("/api/health").status_code == 200
    for method, path in [
        ("get", "/api/files"),
        ("get", "/files/a.txt"),
        ("get", "/api/archive"),
        ("get", "/api/qr.svg"),
        ("delete", "/api/files/a.txt"),
    ]:
        res = getattr(locked, method)(path)
        assert res.status_code == 401, path
    assert upload(locked, "a.txt").status_code == 401
    info = locked.get("/api/info").json
    assert info["auth"] == {"required": True, "authenticated": False, "numeric": True}
    assert "share_url" not in info
    assert "pin" not in info


def test_pin_login(locked: FlaskClient) -> None:
    res = locked.post("/api/auth", json={"pin": "0000"})
    assert res.status_code == 401
    assert res.json["error"]["code"] == "wrong_pin"
    assert locked.post("/api/auth", json={"pin": "4821"}).status_code == 200
    assert locked.get("/api/files").status_code == 200
    assert locked.get("/api/info").json["pin"] == "4821"
    locked.post("/api/logout")
    assert locked.get("/api/files").status_code == 401


def test_pin_in_qr_link_signs_in_and_is_stripped(locked: FlaskClient) -> None:
    res = locked.get("/?pin=4821")
    assert res.status_code == 302
    assert "pin" not in res.headers["Location"]
    assert locked.get("/api/files").status_code == 200


def test_wrong_pin_in_link_does_not_sign_in(locked: FlaskClient) -> None:
    locked.get("/?pin=1111")
    assert locked.get("/api/files").status_code == 401


def test_pin_rate_limited(locked: FlaskClient) -> None:
    for _ in range(5):
        assert locked.post("/api/auth", json={"pin": "0000"}).status_code == 401
    res = locked.post("/api/auth", json={"pin": "4821"})
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1


def test_session_survives_restart_but_not_pin_change(make_app: AppFactory) -> None:
    first = make_app(pin="4821").test_client()
    first.post("/api/auth", json={"pin": "4821"})
    cookie = first.get_cookie("open_transfer")
    assert cookie is not None

    same = make_app(pin="4821").test_client()
    same.set_cookie("open_transfer", cookie.value)
    assert same.get("/api/files").status_code == 200

    changed = make_app(pin="9999").test_client()
    changed.set_cookie("open_transfer", cookie.value)
    assert changed.get("/api/files").status_code == 401


def test_rate_limiter_window() -> None:
    limiter = RateLimiter(attempts=2, window=60)
    assert limiter.retry_after("a") == 0
    limiter.hit("a")
    limiter.hit("a")
    assert limiter.retry_after("a") > 0
    assert limiter.retry_after("b") == 0
    limiter.reset("a")
    assert limiter.retry_after("a") == 0


def test_behind_proxy_uses_forwarded_client_ip(make_app: AppFactory) -> None:
    client = make_app(
        pin="4821", trust_proxy=True, allowed_hosts=("files.example.com",)
    ).test_client()
    headers = {"X-Forwarded-Host": "files.example.com", "X-Forwarded-Proto": "https"}
    for _ in range(5):
        client.post(
            "/api/auth", json={"pin": "0"}, headers={**headers, "X-Forwarded-For": "1.1.1.1"}
        )
    blocked = client.post(
        "/api/auth", json={"pin": "4821"}, headers={**headers, "X-Forwarded-For": "1.1.1.1"}
    )
    assert blocked.status_code == 429
    other = client.post(
        "/api/auth", json={"pin": "4821"}, headers={**headers, "X-Forwarded-For": "2.2.2.2"}
    )
    assert other.status_code == 200


def test_https_public_url_sets_secure_cookie(make_app: AppFactory) -> None:
    app = make_app(public_url="https://files.example.com")
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_pin_in_link_is_rate_limited(locked: FlaskClient) -> None:
    for _ in range(5):
        locked.get("/?pin=0000")
    locked.get("/?pin=4821")  # correct, but over the limit
    assert locked.get("/api/files").status_code == 401
    assert locked.post("/api/auth", json={"pin": "4821"}).status_code == 429


def test_rate_limiter_attempt_is_atomic_under_concurrency() -> None:
    from concurrent.futures import ThreadPoolExecutor

    limiter = RateLimiter(attempts=5, window=60)
    with ThreadPoolExecutor(max_workers=32) as pool:
        allowed = sum(
            1 for wait in pool.map(lambda _: limiter.attempt("ip"), range(64)) if not wait
        )
    assert allowed == 5


def test_log_safe_neutralises_newlines() -> None:
    from open_transfer.security import log_safe

    assert log_safe("a\nFAKE LOG LINE\r") == "a\\nFAKE LOG LINE\\r"


# ------------------------------------------------- QR codes never hold the PIN


def _qr_url(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> str:
    import segno

    seen: list[str] = []
    real = segno.make
    monkeypatch.setattr(segno, "make", lambda url, **kw: (seen.append(url), real(url, **kw))[1])
    assert client.get("/api/qr.svg").status_code == 200
    return seen[0]


def test_share_qr_holds_a_short_lived_token_not_the_pin(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(pin="4821")
    inside = app.test_client()
    inside.post("/api/auth", json={"pin": "4821"})
    url = _qr_url(inside, monkeypatch)
    assert "4821" not in url
    assert "pin=" not in url
    token = url.split("join=", 1)[1]

    scanner = app.test_client()  # a phone camera opening the QR
    res = scanner.get(f"/?join={token}")
    assert res.status_code == 302
    assert "join" not in res.headers["Location"]  # stripped from the address bar
    assert scanner.get("/api/files").status_code == 200


def test_an_intercepted_share_qr_stops_working(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_transfer import app as app_module

    app = make_app(pin="4821")
    inside = app.test_client()
    inside.post("/api/auth", json={"pin": "4821"})
    token = _qr_url(inside, monkeypatch).split("join=", 1)[1]
    expires, mac = token.split(".")

    later = app_module.time.time() + app_module.JOIN_TTL + 1
    monkeypatch.setattr(app_module.time, "time", lambda: later)
    photo = app.test_client()
    photo.get(f"/?join={token}")
    assert photo.get("/api/files").status_code == 401  # expired

    monkeypatch.undo()
    for forged in (f"{int(expires) + 86400}.{mac}", f"{expires}.{'0' * 32}", "garbage"):
        attacker = app.test_client()
        attacker.get(f"/?join={forged}")
        assert attacker.get("/api/files").status_code == 401, forged


def test_changing_the_pin_invalidates_qr_codes(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = make_app(pin="4821", state_dir=tmp_path / "state")
    inside = old.test_client()
    inside.post("/api/auth", json={"pin": "4821"})
    token = _qr_url(inside, monkeypatch).split("join=", 1)[1]
    new = make_app(pin="9999", state_dir=tmp_path / "state")  # same secret key, new PIN
    browser = new.test_client()
    browser.get(f"/?join={token}")
    assert browser.get("/api/files").status_code == 401


def test_share_qr_without_a_pin_is_just_the_address(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _qr_url(make_app().test_client(), monkeypatch)
    assert "?" not in url

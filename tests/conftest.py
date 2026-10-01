from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from open_transfer.app import create_app
from open_transfer.config import Config

AppFactory = Callable[..., Flask]


@pytest.fixture
def make_app(tmp_path: Path) -> AppFactory:
    def factory(**overrides: object) -> Flask:
        overrides.setdefault("storage_dir", tmp_path / "share")
        overrides.setdefault("reserve_disk_bytes", 0)
        # The classic suite tests the shared-folder mode as seen by a visitor;
        # tests/test_mesh*.py cover the owner and nearby devices.
        overrides.setdefault("share_folder", True)
        overrides.setdefault("owner_loopback", False)
        overrides.setdefault("discovery", False)
        app = create_app(Config(**overrides))  # type: ignore[arg-type]
        app.config["TESTING"] = True
        return app

    return factory


@pytest.fixture
def app(make_app: AppFactory) -> Flask:
    return make_app()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def share_dir(app: Flask) -> Path:
    return Path(app.extensions["open_transfer"]["config"].storage_dir)


def upload(client: FlaskClient, name: str, data: bytes = b"hello", **headers: str):
    from urllib.parse import quote

    return client.post(
        "/api/files",
        data=data,
        headers={"X-Filename": quote(name), "Content-Type": "application/octet-stream", **headers},
    )

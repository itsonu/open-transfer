from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from open_transfer import cli, network
from open_transfer.config import Config, parse_size


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("", 0),
        ("100", 100),
        ("500M", 500_000_000),
        ("2GB", 2_000_000_000),
        ("1.5g", 1_500_000_000),
        ("4GiB", 4 * 1024**3),
        ("10kb", 10_000),
    ],
)
def test_parse_size(raw: str, expected: int) -> None:
    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", ["lots", "-5", "5X"])
def test_parse_size_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid size"):
        parse_size(raw)


def test_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PIN"):
        Config(storage_dir=tmp_path, pin="12")
    with pytest.raises(ValueError, match="PIN"):
        Config(storage_dir=tmp_path, pin="12 34")
    with pytest.raises(ValueError, match="port"):
        Config(storage_dir=tmp_path, port=70000)
    assert Config(storage_dir=tmp_path, pin="  ").pin is None


def parse(*argv: str) -> Config:
    return cli.config_from_args(cli.build_parser().parse_args(list(argv)))


def test_cli_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    config = parse()
    assert config.port == 5000
    assert config.storage_dir == (tmp_path / "uploads").resolve()
    assert config.pin is None
    assert config.allow_upload
    assert config.allow_delete
    assert config.allow_browse


def test_cli_flags(tmp_path: Path) -> None:
    config = parse(str(tmp_path), "-p", "8080", "--pin", "abcd", "--max-size", "1G", "--no-delete")
    assert config.storage_dir == tmp_path.resolve()
    assert config.port == 8080
    assert config.pin == "abcd"
    assert config.max_upload_size == 1_000_000_000
    assert not config.allow_delete


def test_cli_auto_pin() -> None:
    pin = parse("--pin").pin
    assert pin is not None
    assert len(pin) == 4
    assert pin.isdigit()


def test_cli_modes() -> None:
    read_only = parse("--read-only")
    assert not read_only.allow_upload
    assert not read_only.allow_delete
    assert not parse("--receive-only").allow_browse


def test_env_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPEN_TRANSFER_DIR", str(tmp_path))
    monkeypatch.setenv("OPEN_TRANSFER_PORT", "9000")
    monkeypatch.setenv("OPEN_TRANSFER_PIN", "7777")
    monkeypatch.setenv("OPEN_TRANSFER_NO_DELETE", "true")
    monkeypatch.setenv("OPEN_TRANSFER_ALLOWED_HOSTS", "a.example,b.example")
    config = parse()
    assert config.storage_dir == tmp_path.resolve()
    assert config.port == 9000
    assert config.pin == "7777"
    assert not config.allow_delete
    assert config.allowed_hosts == ("a.example", "b.example")
    assert parse("--port", "1234").port == 1234  # flags beat env


def test_main_reports_bad_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--pin", "1", "--no-browser"]) == 2
    assert "PIN must be" in capsys.readouterr().err


def test_find_free_port_skips_busy_port() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert network.find_free_port("127.0.0.1", port) != port
    assert network.find_free_port("127.0.0.1", 0) > 0


def test_lan_ips_are_usable() -> None:
    for ip in network.lan_ips():
        assert not ip.startswith("127.")


def test_banner_shows_addresses_pin_and_qr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(network, "lan_ips", lambda: ["192.168.1.24"])
    config = Config(storage_dir=tmp_path, pin="4821")
    import segno

    printed: list[str] = []
    real = segno.make
    monkeypatch.setattr(segno, "make", lambda url, **kw: (printed.append(url), real(url, **kw))[1])
    share = cli._print_banner(config, 5001, cli._Style(False), show_qr=True)
    assert printed == ["http://192.168.1.24:5001"]  # the QR never holds the PIN
    out = capsys.readouterr().out
    assert share == "http://192.168.1.24:5001"
    assert "http://localhost:5001" in out
    assert "http://192.168.1.24:5001" in out
    assert "4821" in out
    assert "Scan with your phone" in out
    assert "\033[" not in out  # no colour codes when disabled


def test_banner_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(network, "lan_ips", lambda: [])
    cli._print_banner(Config(storage_dir=tmp_path), 5000, cli._Style(False), show_qr=True)
    out = capsys.readouterr().out
    assert "only this computer can connect" in out
    assert "Scan" not in out


def test_standalone_app_defaults_to_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert cli.default_storage_dir() == Path("uploads")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli.default_storage_dir() == tmp_path / "Open Transfer"
    (tmp_path / "Downloads").mkdir()
    assert cli.default_storage_dir() == tmp_path / "Downloads" / "Open Transfer"
    assert parse().storage_dir == (tmp_path / "Downloads" / "Open Transfer").resolve()


def test_standalone_app_waits_before_closing_on_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    assert cli.main(["--pin", "1", "--no-browser"]) == 2
    assert prompts
    assert "Press Enter" in prompts[0]

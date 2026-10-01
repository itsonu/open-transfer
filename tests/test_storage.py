from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path

import pytest

from open_transfer.storage import (
    FileBusy,
    IncompleteUpload,
    InsufficientStorage,
    InvalidName,
    NotFound,
    Storage,
    TooLarge,
    file_kind,
    safe_filename,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system.ini", "system.ini"),
        ("  .hidden  ", "hidden"),
        ("Café 照片.txt", "Café 照片.txt"),
        ('bad<>:"|?*name.txt', "bad_______name.txt"),
        ("CON.txt", "_CON.txt"),
        ("CON .txt", "_CON .txt"),
        ("lpt1", "_lpt1"),
        ("trailing dots...", "trailing dots"),
        ("tab\tand\nnewline", "tab_and_newline"),
        ("Café.txt", "Café.txt"),  # NFD → NFC
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "..", "/", "../"])
def test_safe_filename_rejects_empty(raw: str) -> None:
    with pytest.raises(InvalidName):
        safe_filename(raw)


def test_safe_filename_truncates_long_names_keeping_extension() -> None:
    name = safe_filename("é" * 300 + ".jpeg")
    assert name.endswith(".jpeg")
    assert len(name.encode()) <= 240


def test_file_kind() -> None:
    assert file_kind("a.JPG") == "image"
    assert file_kind("movie.mov") == "video"
    assert file_kind("notes") == "other"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "share", trash_ttl=30)


def test_save_and_list(storage: Storage) -> None:
    info = storage.save_stream("a.txt", io.BytesIO(b"abc"), length=3)
    assert info.name == "a.txt"
    assert info.size == 3
    assert [f.name for f in storage.files()] == ["a.txt"]
    assert (storage.root / "a.txt").read_bytes() == b"abc"


def test_duplicate_names_get_suffixes(storage: Storage) -> None:
    names = [storage.save_stream("a.txt", io.BytesIO(b"x")).name for _ in range(3)]
    assert names == ["a.txt", "a (1).txt", "a (2).txt"]
    tars = [storage.save_stream("b.tar.gz", io.BytesIO(b"x")).name for _ in range(2)]
    assert tars == ["b.tar.gz", "b (1).tar.gz"]


def test_listing_hides_state_dir_dotfiles_dirs_and_symlinks(
    storage: Storage, tmp_path: Path
) -> None:
    (storage.root / ".DS_Store").write_bytes(b"x")
    (storage.root / "folder").mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with contextlib.suppress(OSError, NotImplementedError):  # Windows without privilege
        os.symlink(outside, storage.root / "link.txt")
    storage.save_stream("visible.txt", io.BytesIO(b"x"))
    assert [f.name for f in storage.files()] == ["visible.txt"]
    with pytest.raises(NotFound):
        storage.resolve("link.txt")


def test_resolve_rejects_traversal(storage: Storage) -> None:
    for name in ["../x", "..", "", "a/b", ".open-transfer"]:
        with pytest.raises(NotFound):
            storage.resolve(name)


def test_too_large_declared_and_streamed(storage: Storage) -> None:
    with pytest.raises(TooLarge):
        storage.save_stream("a", io.BytesIO(b"x" * 10), length=10, max_size=5)
    with pytest.raises(TooLarge):
        storage.save_stream("a", io.BytesIO(b"x" * 10), max_size=5)
    assert storage.files() == []
    assert list((storage.root / ".open-transfer" / "incoming").iterdir()) == []


def test_incomplete_upload_is_discarded(storage: Storage) -> None:
    with pytest.raises(IncompleteUpload):
        storage.save_stream("a", io.BytesIO(b"abc"), length=10)
    assert storage.files() == []


def test_insufficient_storage(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "s", reserve_bytes=10**18)
    with pytest.raises(InsufficientStorage):
        storage.save_stream("a", io.BytesIO(b"abc"), length=3)


def test_delete_and_restore(storage: Storage) -> None:
    storage.save_stream("a.txt", io.BytesIO(b"abc"))
    token = storage.delete("a.txt")
    assert storage.files() == []
    restored = storage.restore(token)
    assert restored.name == "a.txt"
    assert (storage.root / "a.txt").read_bytes() == b"abc"
    with pytest.raises(NotFound):
        storage.restore(token)


def _busy_for(times: int, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make moving a file fail like Windows does while it's still open elsewhere."""
    real, calls = os.replace, [0]

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls[0] += 1
        if calls[0] <= times:
            raise PermissionError(32, "The process cannot access the file")
        real(src, dst)

    monkeypatch.setattr("open_transfer.storage.os.replace", replace)
    return calls


def test_delete_waits_for_a_file_that_is_briefly_in_use(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.save_stream("a.txt", io.BytesIO(b"abc"))
    calls = _busy_for(2, monkeypatch)  # e.g. a download that just finished
    token = storage.delete("a.txt")
    assert calls[0] == 3
    assert storage.files() == []
    assert storage.restore(token).name == "a.txt"


def test_delete_of_a_file_kept_open_is_a_clean_error(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.save_stream("a.txt", io.BytesIO(b"abc"))
    _busy_for(1000, monkeypatch)
    with pytest.raises(FileBusy) as info:
        storage.delete("a.txt")
    assert info.value.status == 409
    assert [f.name for f in storage.files()] == ["a.txt"]
    assert not any(storage._trash.iterdir())


def test_restore_after_name_reused(storage: Storage) -> None:
    storage.save_stream("a.txt", io.BytesIO(b"old"))
    token = storage.delete("a.txt")
    storage.save_stream("a.txt", io.BytesIO(b"new"))
    assert storage.restore(token).name == "a (1).txt"


def test_trash_is_purged_after_ttl(storage: Storage) -> None:
    import time

    storage.save_stream("a.txt", io.BytesIO(b"abc"))
    token = storage.delete("a.txt")
    storage.purge_trash(now=time.time() + 31)
    with pytest.raises(NotFound):
        storage.restore(token)


def test_restore_rejects_bad_tokens(storage: Storage) -> None:
    for token in ["", "../../etc", "x" * 100]:
        with pytest.raises(NotFound):
            storage.restore(token)


def test_leftover_partial_uploads_are_cleaned(tmp_path: Path) -> None:
    incoming = tmp_path / "s" / ".open-transfer" / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "dead.part").write_bytes(b"x")
    Storage(tmp_path / "s")
    assert not (incoming / "dead.part").exists()


def test_fingerprint_changes_with_content(storage: Storage) -> None:
    before = storage.fingerprint()
    storage.save_stream("a.txt", io.BytesIO(b"abc"))
    assert storage.fingerprint() != before

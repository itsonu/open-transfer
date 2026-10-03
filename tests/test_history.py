from __future__ import annotations

import stat
import sys
import time
from pathlib import Path

import pytest

from open_transfer.history import (
    ACCEPTED,
    CANCELLED,
    COMPLETED,
    DECLINED,
    EXPIRED,
    FAILED,
    OFFERED,
    PARTIAL,
    TRANSFERRING,
    FileRecord,
    History,
    RecipientRecord,
    TransferRecord,
    aggregate_state,
    lifecycle_state,
)


def _record(
    transfer_id: str = "t-1",
    *,
    direction: str = "sent",
    states: tuple[str, ...] = (COMPLETED,),
    names: tuple[str, ...] = ("photo.jpg",),
    created_at: float | None = None,
) -> TransferRecord:
    return TransferRecord(
        transfer_id=transfer_id,
        direction=direction,
        files=[FileRecord(name=n, size=10, mime="image/jpeg") for n in names],
        recipients=[
            RecipientRecord(
                device_id=f"d-{i:08x}",
                device={"name": f"Device {i}", "form": "phone", "platform": "android"},
                state=s,
                bytes_done=10 if s == COMPLETED else 0,
            )
            for i, s in enumerate(states)
        ],
        sender={"id": "d-aaaaaaaa", "name": "Laptop", "form": "computer", "platform": "macos"},
        created_at=time.time() if created_at is None else created_at,
    )


@pytest.fixture
def history(tmp_path: Path) -> History:
    return History(tmp_path / "history.db")


# ------------------------------------------------------------ aggregate state


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([COMPLETED], COMPLETED),
        ([COMPLETED, COMPLETED, COMPLETED], COMPLETED),
        ([COMPLETED, FAILED], PARTIAL),
        ([COMPLETED, DECLINED, FAILED], PARTIAL),
        ([PARTIAL], PARTIAL),
        ([PARTIAL, COMPLETED], PARTIAL),
        ([DECLINED, DECLINED], DECLINED),
        ([EXPIRED], EXPIRED),
        ([CANCELLED, CANCELLED], CANCELLED),
        ([DECLINED, FAILED], FAILED),
        ([FAILED], FAILED),
        ([], FAILED),
        ([OFFERED, COMPLETED], OFFERED),
        ([TRANSFERRING, OFFERED, OFFERED], TRANSFERRING),
        ([ACCEPTED, OFFERED, OFFERED], ACCEPTED),
        ([COMPLETED, COMPLETED, DECLINED], PARTIAL),
        ([EXPIRED, EXPIRED, EXPIRED], EXPIRED),
        ([CANCELLED, DECLINED, CANCELLED], CANCELLED),
        ([DECLINED, EXPIRED], EXPIRED),
        ([TRANSFERRING, ACCEPTED, COMPLETED], ACCEPTED),
        ([TRANSFERRING, FAILED], TRANSFERRING),
    ],
)
def test_aggregate_state(states: list[str], expected: str) -> None:
    assert aggregate_state(states) == expected


def test_old_state_names_map_onto_the_lifecycle() -> None:
    assert lifecycle_state("pending") == OFFERED
    assert lifecycle_state("waiting") == OFFERED
    assert lifecycle_state("receiving") == TRANSFERRING
    assert lifecycle_state("sending") == TRANSFERRING
    assert lifecycle_state("done") == COMPLETED
    assert lifecycle_state("canceled") == CANCELLED
    assert lifecycle_state(FAILED) == FAILED


# ------------------------------------------------------------- persistence


def test_record_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    first = History(path)
    row_id = first.save(_record(states=(COMPLETED, FAILED)))
    first.close()

    again = History(path)
    item = again.get(row_id)
    assert item is not None
    assert item["transfer_id"] == "t-1"
    assert item["direction"] == "sent"
    assert item["state"] == PARTIAL
    assert item["sender"]["name"] == "Laptop"
    assert [r["state"] for r in item["recipients"]] == [COMPLETED, FAILED]
    assert item["recipients"][0]["name"] == "Device 0"
    assert item["files"][0]["name"] == "photo.jpg"
    assert item["finished_at"] is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_database_is_private(tmp_path: Path) -> None:
    History(tmp_path / "history.db")
    assert stat.S_IMODE((tmp_path / "history.db").stat().st_mode) == 0o600


def test_saving_again_updates_the_same_row(history: History) -> None:
    first = history.save(_record(states=(OFFERED,)))
    second = history.save(_record(states=(TRANSFERRING,)))
    assert first == second
    item = history.get(first)
    assert item is not None
    assert item["state"] == TRANSFERRING
    assert len(history.find()) == 1


def test_final_records_are_never_rewritten(history: History) -> None:
    row_id = history.save(_record(states=(COMPLETED,)))
    history.save(_record(states=(FAILED,)))
    item = history.get(row_id)
    assert item is not None
    assert item["state"] == COMPLETED


def test_sent_and_received_records_of_one_transfer_are_separate(history: History) -> None:
    history.save(_record(direction="sent"))
    received = _record(direction="received")
    received.peer_node = "d-aaaaaaaa"
    history.save(received)
    assert {i["direction"] for i in history.find()} == {"sent", "received"}


def test_restart_marks_active_transfers_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    first = History(path)
    row_id = first.save(_record(states=(TRANSFERRING, COMPLETED)))
    first.close()

    again = History(path)
    assert again.interrupt_unfinished() == 1
    item = again.get(row_id)
    assert item is not None
    assert item["state"] == PARTIAL
    assert item["reason_code"] == "app_restart"
    assert item["recipients"][0]["state"] == FAILED
    assert item["recipients"][0]["reason_code"] == "app_restart"
    assert item["recipients"][1]["state"] == COMPLETED


# ---------------------------------------------------------- finding records


def test_filters_and_search(history: History) -> None:
    history.save(_record("a", direction="sent", names=("holiday.jpg",)))
    history.save(_record("b", direction="received", states=(FAILED,), names=("report.pdf",)))
    history.save(_record("c", direction="sent", states=(COMPLETED, DECLINED)))

    assert [i["transfer_id"] for i in history.find(direction="sent")] == ["c", "a"]
    assert [i["transfer_id"] for i in history.find(direction="received")] == ["b"]
    assert {i["transfer_id"] for i in history.find(failed=True)} == {"b", "c"}
    assert [i["transfer_id"] for i in history.find(text="holiday")] == ["a"]
    assert [i["transfer_id"] for i in history.find(text="Laptop")] == ["c", "b", "a"]
    assert history.find(text="100%_") == []
    assert [i["transfer_id"] for i in history.find(device="d-00000001")] == ["c"]


def test_paging_is_newest_first(history: History) -> None:
    now = time.time()
    for n in range(5):
        history.save(_record(f"t{n}", created_at=now + n))
    page = history.find(limit=2)
    assert [i["transfer_id"] for i in page] == ["t4", "t3"]
    last = page[-1]
    nxt = history.find(limit=2, before=(last["created_at"], last["row_id"]))
    assert [i["transfer_id"] for i in nxt] == ["t2", "t1"]


# ------------------------------------------------------ deleting & retention


def test_delete_and_clear_keep_active_transfers(history: History) -> None:
    done = history.save(_record("done"))
    failed = history.save(_record("failed", states=(FAILED,)))
    active = history.save(_record("active", states=(TRANSFERRING,)))

    assert not history.delete(active)
    assert history.clear(completed_only=True) == 1
    assert history.get(done) is None
    assert history.get(failed) is not None
    assert history.clear() == 1
    assert [i["row_id"] for i in history.find()] == [active]


def test_retention_drops_old_and_extra_records(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db", max_rows=3, max_age=3600)
    now = time.time()
    history.save(_record("ancient", created_at=now - 7200))
    history.save(_record("live", states=(TRANSFERRING,), created_at=now - 7200))
    for n in range(4):
        history.save(_record(f"t{n}", created_at=now + n))

    history.prune(now=now)
    assert {i["transfer_id"] for i in history.find()} == {"live", "t3", "t2", "t1"}


def test_many_records_stay_fast(history: History) -> None:
    now = time.time()
    for n in range(2000):
        history.save(_record(f"t{n}", names=(f"file-{n}.bin",), created_at=now + n))
    started = time.perf_counter()
    page = history.find(text="file-1999", limit=50)
    assert [i["transfer_id"] for i in page] == ["t1999"]
    assert len(history.find(limit=200)) == 200
    assert time.perf_counter() - started < 1.0

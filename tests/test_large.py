"""A big encrypted file to a group: streamed (memory stays bounded however big it
is), progress only moves forward and ends at the size, and it arrives intact."""

from __future__ import annotations

import hashlib
import os
import threading
import tracemalloc
from pathlib import Path

from tests.conftest import NodeFactory
from tests.test_encrypted_transfers import paired
from tests.test_mesh import owner, target_states, upload, wait_for
from tests.test_moves import pair

SIZE = 48 * 1024 * 1024


def test_a_big_encrypted_group_send_streams_and_reports_progress(make_node: NodeFactory) -> None:
    a, b = paired(make_node)
    c = make_node("Charlie")
    pair(a, c)
    wait_for(lambda: a.mesh.trust.get(c.mesh.id).enc)  # type: ignore[union-attr]
    payload = os.urandom(SIZE)
    alice = owner(a)
    status, data = alice(
        "POST",
        "/api/send",
        {"to": [b.mesh.id, c.mesh.id], "files": [{"name": "big.bin", "size": SIZE}]},
    )
    assert status == 201, data
    job = data["job"]["id"]
    wait_for(lambda: set(target_states(alice, job).values()) == {"accepted"})

    seen: dict[str, list[int]] = {"Bravo": [], "Charlie": []}
    done = threading.Event()

    def watch() -> None:  # what each receiver's page would show, as it goes
        while not done.is_set():
            for node, name in ((b, "Bravo"), (c, "Charlie")):
                for s in owner(node).state()["incoming"]:
                    if s["state"] in {"accepted", "receiving", "done"}:
                        seen[name].append(sum(f.get("received", 0) for f in s["files"]))
            done.wait(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    tracemalloc.start()
    watcher.start()
    try:
        status, _ = upload(alice, job, 0, payload)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        done.set()
        watcher.join()
    assert status == 200
    wait_for(lambda: target_states(alice, job) == {"Bravo": "done", "Charlie": "done"}, timeout=30)
    # Streamed through 64 KiB records, not held whole (the test's own copy isn't traced).
    assert peak < 24 * 1024 * 1024, f"peak {peak / 1e6:.0f} MB for a {SIZE / 1e6:.0f} MB file"
    for name, values in seen.items():
        assert len(set(values)) > 2, f"{name}'s progress was never seen moving"
        assert values == sorted(values), f"{name}'s progress went backwards"
        assert all(v <= SIZE for v in values), f"{name}'s progress overshot"
    want = hashlib.sha256(payload).hexdigest()
    for node in (b, c):
        got = hashlib.sha256((Path(node.config.storage_dir) / "big.bin").read_bytes()).hexdigest()
        assert got == want

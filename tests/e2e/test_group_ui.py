"""One card for a group send: who got it, who declined, who dropped out."""

from __future__ import annotations

from pathlib import Path

from tests import test_mesh
from tests.e2e.conftest import sync_api
from tests.test_mesh import NodeFactory, connect, incoming, owner

expect = sync_api.expect
make_node = test_mesh.make_node  # the shared pytest fixture


def test_group_send_shows_one_card_with_a_result_per_device(
    make_node: NodeFactory, page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    alpha = make_node("Alpha")
    others = {name: make_node(name) for name in ("Bravo", "Charlie", "Delta")}
    for node in others.values():
        connect(alpha, node)
    sample = tmp_path / "Holiday photo.jpg"
    sample.write_bytes(b"x" * 300_000)

    page.goto(alpha.local_url)
    for name in others:
        tile = page.locator(".device", has_text=name)
        expect(tile).to_be_visible(timeout=10_000)
        tile.locator(".device-button").click()
    page.set_input_files("#file-input", str(sample))
    page.click("#send-go")
    page.click("#confirm-ok")  # "Send to 3 devices?"

    card = page.locator(".job")
    expect(card.locator(".row-title")).to_have_text("Sending Holiday photo.jpg to 3 devices")
    expect(card.locator(".job-target")).to_have_count(3)

    bravo, charlie, delta = (owner(others[n]) for n in ("Bravo", "Charlie", "Delta"))
    bravo("POST", f"/api/incoming/{incoming(bravo)['id']}/accept")
    charlie("POST", f"/api/incoming/{incoming(charlie)['id']}/decline")
    delta("POST", f"/api/incoming/{incoming(delta)['id']}/accept")
    others["Delta"].stop()  # drops off before the bytes come

    expect(card.locator(".row-title")).to_have_text("Delivered to 1 of 3", timeout=20_000)
    expect(card.locator(".row-sub")).to_have_text("Holiday photo.jpg · 1 failed · 1 declined")
    expect(card.locator(".job-target", has_text="Bravo")).to_contain_text("Delivered")
    expect(card.locator(".job-target", has_text="Charlie")).to_contain_text("Declined")
    expect(card.locator(".job-target", has_text="Delta")).to_contain_text("Failed — Disconnected")
    expect(page.locator(".toast")).to_contain_text("Delivered to 1 of 3")
    assert (Path(others["Bravo"].config.storage_dir) / "Holiday photo.jpg").exists()

    card.locator(".job-details > summary").click()  # the details fold away
    expect(card.locator(".job-target", has_text="Bravo")).to_be_hidden()

"""A nearby device that doesn't answer (its firewall) says so on its tile, and the
pairing sheet's address field can't be mistaken for one that's filled in."""

from __future__ import annotations

import json
import re

import pytest

from tests.e2e.test_devices_ui import tile
from tests.e2e.test_ui import Server, sync_api

pytestmark = pytest.mark.e2e
expect = sync_api.expect


def test_a_device_behind_a_firewall_says_so(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    def blocked(route) -> None:  # type: ignore[no-untyped-def]
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        data = response.json()
        for d in data.get("devices", []):
            if d.get("name") == "Bravo":
                d["reachable"] = False  # what the app reports when hellos get no answer
        route.fulfill(response=response, body=json.dumps(data))

    page.route("**/api/state*", blocked)
    page.goto(devices[0].url)
    bravo = tile(page, "Bravo")
    expect(bravo.locator(".device-sub")).to_have_text("Can’t connect", timeout=10_000)
    expect(bravo.locator(".device-button")).to_have_attribute("title", re.compile("firewall"))
    expect(tile(page, "Charlie").locator(".device-sub")).not_to_have_text("Can’t connect")


def test_the_address_field_is_clearly_empty(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    page.goto(devices[0].url)
    page.click("#connect-button")
    page.click("#connect-tabs [data-tab=enter]")
    page.click("#pair-address-wrap summary")
    field = page.locator("#pair-address")
    expect(field).to_have_value("")
    expect(field).to_have_attribute("placeholder", re.compile("^Its address"))
    expect(page.locator(".pair-address-hint")).to_contain_text("Show code")


def test_a_pc_on_a_public_network_is_told(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    def public(route) -> None:  # type: ignore[no-untyped-def]
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        data = response.json()
        data["network"] = {"public": ["Home-5G"]}  # what a Windows PC reports
        route.fulfill(response=response, body=json.dumps(data))

    page.route("**/api/state*", public)
    page.goto(devices[0].url)
    status = page.locator("#me-status")
    expect(status).to_contain_text("Public network", timeout=10_000)
    expect(status).to_contain_text("Home-5G")
    expect(status).to_have_attribute("data-tone", "warn")


def _network(page, network: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    def patch(route) -> None:  # type: ignore[no-untyped-def]
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        data = response.json()
        data["network"] = network
        route.fulfill(response=response, body=json.dumps(data))

    page.route("**/api/state*", patch)


def test_a_pc_others_cant_reach_is_told_by_name(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    """K1: Private network, but its firewall rule only allows Public. The Mac says so."""
    _network(page, {"public": [], "unreachable_by": ["Sonu’s Mac"], "reachable_by": 0})
    page.goto(devices[0].url)
    status = page.locator("#me-status")
    expect(status).to_contain_text("Sonu’s Mac can’t reach this device", timeout=10_000)
    expect(status).to_contain_text("firewall")
    expect(status).to_have_attribute("data-tone", "warn")


def test_the_public_network_guess_gives_way_to_evidence(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    """Allowed on Public networks too: others do reach it, so no warning."""
    _network(page, {"public": ["Home-5G"], "unreachable_by": [], "reachable_by": 1})
    page.goto(devices[0].url)
    status = page.locator("#me-status")
    expect(status).to_have_text(re.compile("discoverable|Visible"), timeout=10_000)
    expect(status).not_to_contain_text("Public network")


def test_the_windows_app_offers_to_allow_it(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    def patch(route) -> None:  # type: ignore[no-untyped-def]
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        data = response.json()
        data["me"]["platform"] = "windows"
        data["network"] = {
            "public": [],
            "unreachable_by": ["Mac"],
            "reachable_by": 0,
            "can_allow": True,
        }
        route.fulfill(response=response, body=json.dumps(data))

    asked: list[str] = []

    def allow(route) -> None:  # type: ignore[no-untyped-def]
        asked.append(route.request.method)
        route.fulfill(status=200, content_type="application/json", body='{"result": "allowed"}')

    page.route("**/api/state*", patch)
    page.route("**/api/network/allow", allow)
    page.goto(devices[0].url)
    button = page.locator("#me-allow")
    expect(button).to_be_visible(timeout=10_000)
    expect(button).to_have_text("Allow on this network")
    button.click()
    expect(page.locator(".toast", has_text="Allowed")).to_be_visible()
    assert asked == ["POST"]


def test_no_allow_button_without_the_windows_app(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    _network(page, {"public": [], "unreachable_by": ["Mac"], "reachable_by": 0, "can_allow": False})
    page.goto(devices[0].url)
    expect(page.locator("#me-status")).to_contain_text("can’t reach", timeout=10_000)
    expect(page.locator("#me-allow")).to_be_hidden()


def test_a_windows_block_rule_is_named_and_offered_for_removal(page, devices: list[Server]) -> None:  # type: ignore[no-untyped-def]
    """Windows' own prompt, cancelled once, left a Block rule for the app: Block beats
    Allow, so the button says it removes that rule too (only that one)."""

    def patch(route) -> None:  # type: ignore[no-untyped-def]
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        data = response.json()
        data["me"]["platform"] = "windows"
        data["network"] = {
            "public": [],
            "unreachable_by": ["Mac"],
            "reachable_by": 0,
            "can_allow": True,
            "blocking_rules": ["open-transfer-windows-x64.exe"],
        }
        route.fulfill(response=response, body=json.dumps(data))

    page.route("**/api/state*", patch)
    page.goto(devices[0].url)
    status = page.locator("#me-status")
    expect(status).to_contain_text("open-transfer-windows-x64.exe", timeout=10_000)
    expect(status).to_contain_text("blocks Open Transfer")
    expect(page.locator("#me-allow")).to_have_text("Remove the block and allow")

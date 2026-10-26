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

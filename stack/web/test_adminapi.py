"""The admin API's views, pure: each names the fields it passes on, so nothing secret can
ride along, and a PATCH is checked as the Places forms check theirs."""

import json
from datetime import datetime, timezone

import pytest

import adminapi

ALPHA = "5YJ3E1EA0JF000001"


def test_a_source_view_names_its_fields_so_a_new_secret_cannot_ride_along():
    row = {"id": 1, "kind": "teslafi", "vin": ALPHA, "label": "Alpha", "config": {"token": "x"}, "plain": "tf-SECRET", "enabled": True, "last_ok_at": None, "last_error": None, "password": "pw"}
    out = adminapi.source(row, "Alpha", {"teslafi": "TeslaFi"})
    assert set(out) == {"id", "kind", "kind_label", "vin", "car", "label", "enabled", "readable", "last_ok_at", "last_error", "status"}


def test_a_source_error_is_masked_with_its_own_secret():
    row = {
        "id": 1,
        "kind": "teslamate",
        "vin": None,
        "label": "TeslaMate",
        "config": {"vins": [ALPHA]},
        "plain": "postgresql://tm:pwS3c@db/tm",
        "last_error": "refused postgresql://tm:pwS3c@db/tm",
    }
    out = adminapi.source(row, "TeslaMate", {}, (None, "lost pwS3c"))
    assert "pwS3c" not in json.dumps(out) and out["following"] == [ALPHA]


def test_a_tesla_view_never_passes_on_an_unnamed_fact():
    facts = {
        "app_ok": True,
        "access_token": "acc-SECRET",
        "pem": "PRIVATE KEY",
        "cars": [{"vin": ALPHA, "name": "Alpha", "paired": True, "refresh_token": "ref-SECRET"}],
        "receiver": {"ok": True, "cert": "PRIVATE KEY"},
    }
    text = json.dumps(adminapi.tesla(facts, [], "example.test", "example.test"))
    assert "SECRET" not in text and "PRIVATE" not in text


def test_a_test_result_keeps_what_it_found_and_nothing_else():
    assert adminapi.tested({"ok": True, "vin": ALPHA, "name": "Alpha", "battery": 80, "token": "x"}) == {"ok": True, "error": None, "vin": ALPHA, "name": "Alpha"}
    assert adminapi.tested({"ok": True, "cars": [{"vin": ALPHA, "name": None, "model": "3"}]})["cars"] == [{"vin": ALPHA, "name": ""}]


def test_plain_turns_times_into_text():
    assert adminapi.plain({"at": datetime(2026, 10, 4, tzinfo=timezone.utc), "t": (1, 2)}) == {"at": "2026-10-04T00:00:00+00:00", "t": [1, 2]}


def _icon(icon):
    if icon in (None, "", "none", "work"):
        return icon or None
    raise ValueError("Not a recognised icon.")


def test_a_patch_becomes_the_changes_rename_place_takes():
    assert adminapi.place_patch({"name": " Work  Place ", "icon": "work"}, _icon) == {"name": "Work Place", "icon": "work"}
    assert adminapi.place_patch({"price": {"kind": "minute", "rate": "0.25"}}, _icon) == {"price": (True, {"kind": "minute", "rate": 0.25})}
    assert adminapi.place_patch({"price": None, "address": None}, _icon) == {"price": (True, None), "address": (True, None)}
    assert adminapi.place_patch({"address": "  2  Side St "}, _icon) == {"address": (True, "2 Side St")}
    assert adminapi.place_patch({"name": "x" * 60}, _icon)["name"] == "x" * 40


@pytest.mark.parametrize("body", [None, [], {}, {"nick": "x"}, {"name": None}, {"price": {"kind": "kwh", "rate": 0}}, {"address": 5}, {"icon": 3}])
def test_a_bad_patch_is_a_bad_request(body):
    with pytest.raises(adminapi.BadRequest):
        adminapi.place_patch(body, _icon)


def test_a_plan_is_summed_up_by_its_newest_prices():
    plan = {
        "name": "Springfield EV",
        "source": "manual",
        "versions": [
            {"from": "2026-01-01", "periods": [{"key": "peak", "label": "Peak", "price": 0.5}]},
            {"from": "2025-01-01", "periods": [{"key": "peak", "price": 0.4}]},
        ],
    }
    out = adminapi.plan_summary(plan, "peak 4pm–9pm")
    assert out["from"] == "2026-01-01" and out["periods"] == [{"key": "peak", "label": "Peak", "price": 0.5}] and out["versions"] == 2
    assert adminapi.plan_summary(None) is None


def test_money_carries_the_fast_prices_and_averages_only():
    out = adminapi.money({"currency": "€", "dc_price": 0.5, "openei_key": "k-SECRET"}, None, "", {"origin": "bundled", "url": "https://example.test"})
    assert out["currency"] == "€" and out["fast"]["dc_price"] == 0.5 and out["plan"] is None
    assert "SECRET" not in json.dumps(out) and "url" not in out["averages"]

"""Settings › Cars and sources: plain source names, the pack's origin, accounts and cards."""

from datetime import datetime, timezone

import carspage
from carspage import accounts, cards, live_name, model_figure, pack, row_status, writer_name

T = datetime(2026, 9, 28, 18, tzinfo=timezone.utc)


def test_sources_have_plain_names():
    assert writer_name("feed") == "TeslaFi (API)" and writer_name("teslamate") == "TeslaMate" and writer_name("tesla") == "Tesla (direct)"
    assert writer_name("live", "Lucid") == "Lucid (CarConnectivity)" and writer_name("live", "Volkswagen") == "Volkswagen (CarConnectivity)" and live_name(None) == "CarConnectivity"
    for name in list(carspage.WRITERS.values()) + list(carspage.FEEDS.values()):
        assert "connector" not in name and not name.startswith("the ")


def test_the_pack_says_where_it_came_from():
    assert pack(117.1, 117.1, "Lucid", None)["text"] == "reported by Lucid"
    assert pack(75.0, None, None, 75.0)["text"] == "the model's figure"
    you = pack(78.1, None, None, 75.0)
    assert you["origin"] == "you" and you["text"] == "set by you" and you["figure"] == 75.0 and you["figure_by"] == "the model's figure"
    assert pack(None, None, None, 75.0)["text"] == "unknown"
    assert pack(110.0, 117.1, "Lucid", None)["figure_by"] == "Lucid's figure", "what the car reports wins over the model"
    assert model_figure("Tesla", "Model 3") == 75.0 and model_figure("Lucid", "Air") is None


def test_row_status_reads_as_a_dot_and_a_sentence():
    assert row_status("teslafi", status=(T, None)) == {"state": "ok", "text": "Working", "at": T}
    assert row_status("teslafi", status=(None, "Refused"))["state"] == "bad"
    assert row_status("teslafi", readable=False)["text"].startswith("Saved with another key")
    assert row_status("lucid", pending=True)["state"] == "wait"
    assert row_status("lucid", status=(None, "The connector reports no car."))["text"] == "The connector reports no car."
    assert row_status("teslamate")["state"] == "none"


def test_accounts_and_cards():
    rows = [
        {"id": 1, "kind": "teslafi", "vin": "V1", "label": "Alpha", "config": {}, "readable": True, "status": (T, None), "last_ok_at": None, "last_error": None},
        {"id": 2, "kind": "teslamate", "vin": None, "label": "TeslaMate", "config": {"vins": ["V1", "V2"]}, "readable": True, "status": None, "last_ok_at": None, "last_error": None},
    ]
    env = [{"kind": "teslafi", "vin": "V3", "car": "Charlie", "status": None, "used": False}]
    names = {"V1": "Alpha", "V2": "Bravo", "V3": "Charlie"}
    accts = accounts(rows, env, True, names, ["V3"], [], (T, None))
    assert [a["what"] for a in accts] == ["TeslaFi token", "TeslaMate database", "Lucid account", "TeslaFi token"]
    assert accts[1]["cars"] == ["Alpha", "Bravo"] and accts[2]["kept"] == "from a sign-in file" and accts[3]["kept"] == "from an older token setting, no longer read"
    assert accts[3]["vins"] == [], "a .env source no longer read feeds no car"
    vehicles = [{"vin": "V1", "name": "Alpha"}, {"vin": "V3", "name": "Charlie"}]
    got = cards(vehicles, {"V1": ("Tesla", "Model 3"), "V3": ("Lucid", "Air")}, {"V1": "feed", "V3": "live"}, {"V1": (T, None), "V3": (T, None)}, {"V1": T}, {}, accts)
    alpha, charlie = got
    assert [s["name"] for s in alpha["sources"]] == ["TeslaFi (API)", "TeslaMate"] and alpha["sources"][0]["writes"] and not alpha["sources"][1]["writes"]
    assert alpha["model"] == "Tesla Model 3" and alpha["newest"] == T and alpha["pack"]["text"] == "unknown"
    assert [s["name"] for s in charlie["sources"]] == ["Lucid (CarConnectivity)"], "the Lucid account is the same source the car is written from"


def test_another_brand_is_named_for_its_brand_and_says_how_its_install_went():
    from carspage import cc_status
    from connectors import get, row_config

    vw = row_config(get("volkswagen"))
    assert cc_status(vw)["text"] == "Installing carconnectivity-connector-volkswagen 0.10.6…" and cc_status(vw)["state"] == "wait"
    assert cc_status({**vw, "state": "ready"})["text"].startswith("Starting CarConnectivity")
    assert cc_status({**vw, "state": "failed"}, last_error="pip could not install it")["state"] == "bad"
    assert cc_status({**vw, "state": "remove"})["text"].startswith("Removing")
    assert cc_status({**vw, "state": "running"}, status=(T, None), has_cars=True) == {
        "state": "ok",
        "text": "Working",
        "at": T,
    }
    assert "no car" in cc_status({**vw, "state": "running"}, status=(T, None), has_cars=False)["text"], "the connector's other cars say nothing about this account"
    rows = [
        {
            "id": 5,
            "kind": "carconnectivity",
            "vin": None,
            "label": "Volkswagen (Europe)",
            "config": {**vw, "state": "running"},
            "readable": True,
            "status": (T, None),
            "last_ok_at": T,
            "last_error": None,
        }
    ]
    accts = accounts(
        rows,
        [],
        False,
        {"V1": "Alpha", "V3": "Charlie"},
        ["V3"],
        [],
        (T, None),
        (),
        {"V1": "Volkswagen", "V3": "Lucid"},
    )
    assert accts[0]["what"] == "Volkswagen (Europe) (CarConnectivity)" and accts[0]["vins"] == ["V1"] and accts[0]["cars"] == ["Alpha"] and accts[0]["state"] == "ok"
    alpha, charlie = cards(
        [{"vin": "V1", "name": "Alpha"}, {"vin": "V3", "name": "Charlie"}],
        {"V1": ("Volkswagen", "ID.4"), "V3": ("Lucid", "Air")},
        {"V1": "live", "V3": "live"},
        {"V1": (T, None), "V3": (T, None)},
        {},
        {},
        accts,
    )
    assert [s["name"] for s in alpha["sources"]] == ["Volkswagen (CarConnectivity)"] and [s["name"] for s in charlie["sources"]] == ["Lucid (CarConnectivity)"]


def test_a_brand_waiting_to_retry_says_why_and_when():
    from carspage import cc_status
    from connectors import get, row_config

    got = cc_status(
        {**row_config(get("audi")), "state": "retry"},
        last_error="pip could not install it: no network. Trying again in 2 minutes.",
    )
    assert got["state"] == "bad" and "Trying again in 2 minutes" in got["text"]

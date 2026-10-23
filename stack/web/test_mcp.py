"""The read-only MCP server: the protocol, every tool on demo-like data, what a token may see, and the
wiring through the site's guard. Cars are Alpha, Bravo and Charlie in Springfield."""

import importlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import mcpserver
import mcptools
from mcptools import Data, Scope

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 13, 9, 30, tzinfo=TZ)
ALPHA, BRAVO = "ALPHA000000000001", "BRAVO000000000002"
CARS = [{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}]
HOME = {"id": 1, "name": "Home", "auto": False, "latitude": 39.7817, "longitude": -89.6501}
STREET = {"id": 2, "name": "Elm Street, Springfield", "auto": True, "latitude": 39.8, "longitude": -89.65}


def drive_row(i=7, vin=ALPHA, car="Alpha", named_end=True):
    t0 = NOW - timedelta(hours=20)
    return {
        "id": i,
        "vin": vin,
        "car_name": car,
        "start_date": t0,
        "destination_date": t0 + timedelta(minutes=25),
        "duration": timedelta(minutes=25),
        "miles": 12.34,
        "start_odometer": 1000.0,
        "destination_odometer": 1012.34,
        "from": "Home",
        "from_ref": {"name": "Home", "lat": 39.7817, "lon": -89.6501, "new": False},
        "start_city": "Springfield",
        "to": "12 Elm Street, Springfield" if not named_end else "Work",
        "to_ref": {"name": "x", "lat": 39.8, "lon": -89.65, "new": not named_end},
        "dest_city": "Springfield",
        "kwh_used": 3.456,
        "wh_per_mi": 280.1,
        "level_start": 80,
        "level_end": 72,
        "cost": 0.5189,
        "cost_estimate": True,
        "max_mph": 61.0,
        "temp_f": 48.2,
        "weather": "light rain",
        "rated_used": 11.0,
        "elev_max_m": 200.0,
        "elev_min_m": 150.0,
        "note": "commute",
        "start_position_latitude": 39.7817,
        "start_position_longitude": -89.6501,
        "destination_position_latitude": 39.8,
        "destination_position_longitude": -89.65,
        "tag": "Commute",
    }


def charge_row(i=3):
    t0 = NOW - timedelta(days=2)
    return {
        "id": i,
        "vin": ALPHA,
        "car_name": "Alpha",
        "session_start_date": t0,
        "session_end_date": t0 + timedelta(hours=2),
        "duration": timedelta(hours=2),
        "where": "Home",
        "where_ref": {"name": "Home", "lat": 1, "lon": 2, "new": False},
        "city": "Springfield",
        "charging_type": "AC",
        "start_level": 40,
        "end_level": 80,
        "kwh": 28.04,
        "cost": 3.51,
        "cost_estimate": False,
        "at_home": True,
        "range_added": 120.0,
        "rate_plan": "Springfield EV",
        "cost_source": "plan",
        "off_peak_kwh": 28.04,
        "src_solar_kwh": 10.0,
        "session_position_latitude": 39.7817,
        "session_position_longitude": -89.6501,
    }


def month_row(day, miles=100.0, cost=12.0):
    return {"period": day, "drives": 10, "miles": miles, "drive_h": 5.0, "used_kwh": 30.0, "drive_cost": 3.0, "charges": 4, "added_kwh": 40.0, "cost": cost, "priced": 3, "avg_cost": 2.0}


def fake(calls=None):
    calls = calls if calls is not None else []

    def log(name, rows):
        def run(*a):
            calls.append((name, a))
            return rows(*a) if callable(rows) else rows

        return run

    live = {
        ALPHA: {
            "vin": ALPHA,
            "status": "Charging",
            "soc": 61,
            "range_mi": 180.4,
            "plugged": True,
            "charging_power_kw": 11.0,
            "charge_target": 80,
            "charge_minutes_left": 95,
            "locked": True,
            "odometer_mi": 12000.4,
            "outside_f": 55.1,
            "software": "2026.8.1",
            "place_named": "Home",
            "locality": "Springfield",
            "at_home": True,
            "updated_age": "2 min",
            "lat": 39.7817,
            "lon": -89.6501,
            "address": "1 Elm St, Springfield",
            "speed_mph": 0,
            "heading": 90,
        },
        BRAVO: {
            "vin": BRAVO,
            "status": "Parked",
            "soc": 40,
            "range_mi": 100.0,
            "plugged": False,
            "locked": False,
            "odometer_mi": 500.0,
            "place_named": None,
            "locality": "Shelbyville",
            "at_home": False,
            "updated_age": "1 h",
            "lat": 1.0,
            "lon": 2.0,
        },
    }
    rep = {
        "now": 301.2,
        "first": 310.0,
        "best": {"full": 312.0},
        "charges": 41,
        "first_at": datetime(2025, 1, 5, 18, tzinfo=timezone.utc),
        "now_at": datetime(2026, 10, 10, 18, tzinfo=timezone.utc),
        "odo": (900.0, 12000.0),
        "losses": {"best": {"from": 312.0, "to": 301.2, "lost": 10.8, "pct": 0.0346}, "first": {"from": 310.0, "to": 301.2, "lost": 8.8, "pct": 0.0284}, "start": None},
    }
    board = [
        {
            "kind": "drive",
            "row": drive_row(),
            "titles": [
                {"key": "distance", "label": "Longest drive", "value": 12.34, "fmt": "dist", "note": None},
                {"key": "best_wh", "label": "Most efficient", "value": 280.1, "fmt": "econ", "note": 10.0},
            ],
        },
        {"kind": "charge", "row": charge_row(), "titles": [{"key": "peak_kw", "label": "Fastest charge", "value": 11.0, "fmt": "kw", "note": None}]},
    ]
    routes = {
        "routes": [
            {"a": HOME, "b": {"id": 3, "name": "Work", "auto": False}, "loop": False, "n": 40, "minutes": 24.6, "wh_per_mi": 270.0, "last": NOW, "tag": "Commute"},
            {"a": HOME, "b": STREET, "loop": False, "n": 9, "minutes": 10.0, "last": NOW},
        ],
        "destinations": [{"place": HOME, "n": 80, "last": NOW}, {"place": STREET, "n": 9, "last": NOW}],
        "drives": 200,
    }
    soft = {
        "cadence": {"every": 21.5, "since": 4},
        "rows": [{"version": "2026.8.1", "seen_at": NOW - timedelta(days=4), "days": 4, "odometer": 11900.0, "full": 301.0}, {"version": "2026.2.3", "seen_at": NOW - timedelta(days=90), "days": 86}],
    }
    return Data(
        cars=lambda: CARS,
        status=log("status", lambda vin: live[vin]),
        drives=log("drives", lambda vin, limit, lo, hi: [drive_row(i, named_end=i % 2 == 1) for i in range(limit)] if limit < 5 else [drive_row()] * 3),
        charges=log("charges", lambda vin, limit, lo, hi: [charge_row()]),
        drive=log("drive", lambda vin, i: drive_row(i) if i == 7 else None),
        charge=log("charge", lambda vin, i: charge_row(i) if i == 3 else None),
        summarize=log("summarize", [month_row(date(2026, 9, 1)), month_row(date(2026, 10, 1), 50.0, 6.0)]),
        lifetime=log("lifetime", lambda vin: rep if vin == ALPHA else None),
        records=log("records", board),
        routes=log("routes", routes),
        software=log("software", soft),
        now=lambda: NOW,
        tz=TZ,
        currency="$",
    )


def call(data, name, args=None, scope=Scope()):
    _, reply = mcpserver.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}, {}, mcptools.build(data), scope, {"name": "t", "version": "0"})
    result = reply["result"]
    body = result["content"][0]["text"]
    return result["isError"], (body if result["isError"] else json.loads(body))


# ---- the protocol -------------------------------------------------------------------------------


def rpc(message, tools=None, scope=Scope(), headers=None, status=False):
    code, body = mcpserver.respond(message, headers or {}, tools if tools is not None else mcptools.build(fake()), scope, {"name": "car-command", "version": "9"}, "hello")
    return (code, body) if status else body


def test_the_handshake_names_the_server_and_agrees_a_version():
    r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}})
    res = r["result"]
    assert r["id"] == 1 and res["protocolVersion"] == "2025-03-26" and res["serverInfo"] == {"name": "car-command", "version": "9"}
    assert res["capabilities"] == {"tools": {"listChanged": False}} and res["instructions"] == "hello"
    newest = rpc({"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": {"protocolVersion": "2031-01-01"}})
    assert newest["result"]["protocolVersion"] == "2025-11-25" and newest["id"] == "a"


def test_a_notification_gets_no_reply_and_ping_gets_an_empty_one():
    assert rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc({"jsonrpc": "2.0", "id": 5, "method": "ping"}) == {"jsonrpc": "2.0", "id": 5, "result": {}}
    assert rpc({"jsonrpc": "2.0", "id": 5, "result": {}}) is None, "a client's reply to us needs nothing"


def test_malformed_messages_are_errors_not_crashes():
    for bad in (
        [],
        "hi",
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
    ):
        code, body = rpc(bad, status=True)
        assert code == 400 and body["error"]["code"] == mcpserver.INVALID_REQUEST, bad
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"] == mcpserver.NO_METHOD
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []})["error"]["code"] == mcpserver.BAD_PARAMS


def test_a_batch_is_refused_and_never_recursed_into():
    nested = [[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]]
    for batch in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], nested, []):
        code, body = rpc(batch, status=True)
        assert code == 400 and body["error"]["code"] == mcpserver.INVALID_REQUEST and body["id"] is None


def test_tools_list_names_every_tool_and_marks_each_read_only():
    tools = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "list_cars",
        "current_status",
        "list_drives",
        "list_charges",
        "get_drive",
        "get_charge",
        "stats",
        "battery_health",
        "records",
        "top_routes",
        "top_places",
        "software_versions",
    ]
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        assert t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False and t["description"]
    assert not any(w in t["name"] for t in tools for w in ("command", "set_", "delete", "send", "write")), "nothing here can change a car"


def test_bad_arguments_come_back_as_a_tool_error_the_model_can_read():
    d = fake()
    assert call(d, "stats", {"nope": 1}) == (True, "unknown argument 'nope'; this tool takes car, period, since, until, by")
    assert call(d, "list_drives", {"limit": "5"})[0] and call(d, "list_drives", {"limit": 999})[0]
    assert call(d, "stats", {"by": "decade"})[1].startswith("by must be one of")
    assert call(d, "get_drive", {"id": True})[0]
    assert call(d, "list_drives", {"car": "x" * 500})[0]
    r = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "drive_the_car"}})
    assert r["error"]["code"] == mcpserver.BAD_PARAMS, "an unknown tool is a protocol error"


def test_a_tool_that_breaks_is_one_failed_call():
    d = fake()
    d.status = lambda vin: 1 / 0
    err, text = call(d, "current_status")
    assert err and "failed on the server" in text and "division" not in text, "no internals leak"


# ---- the tools ----------------------------------------------------------------------------------


def test_list_cars_current_status_and_the_place_it_says():
    d = fake()
    assert call(d, "list_cars") == (False, [{"name": "Alpha"}, {"name": "Bravo"}]), "no VIN goes to the model"
    assert call(d, "list_cars", {}, Scope(coords=True))[1][0]["vin"] == ALPHA
    err, st = call(d, "current_status")
    a, b = st
    assert not err and a["car"] == "Alpha" and a["state"] == "Charging" and a["battery_pct"] == 61 and a["place"] == "Home" and a["at_home"] is True
    assert a["charge_target_pct"] == 80 and a["minutes_to_target"] == 95 and a["plugged_in"] is True
    assert b["place"] == "Shelbyville" and "charging_kw" not in b, "an unnamed place is the city, and absent figures are left out"
    assert call(d, "current_status", {"car": "bravo"})[1][0]["car"] == "Bravo"
    assert call(d, "current_status", {"car": BRAVO.lower()})[1][0]["car"] == "Bravo", "a VIN works, in any case"
    assert "no car called" in call(d, "current_status", {"car": "Delta"})[1] and "Alpha, Bravo" in call(d, "current_status", {"car": "Delta"})[1]


def test_drives_are_compact_paged_and_name_places_not_streets():
    d = fake()
    err, out = call(d, "list_drives", {"period": "yesterday", "limit": 2})
    assert not err and out["period"] == "yesterday" and out["next_offset"] == 2 and len(out["rows"]) == 2
    row = out["rows"][1]  # i=1: a named end
    assert row == {
        "id": 1,
        "car": "Alpha",
        "start": "2026-10-12 13:30",
        "minutes": 25,
        "miles": 12.3,
        "from": "Home",
        "to": "Work",
        "kwh": 3.5,
        "wh_per_mile": 280,
        "battery": [80, 72],
        "cost": 0.52,
        "tag": "Commute",
    }
    assert out["rows"][0]["to"] == "Springfield", "a made-up street name becomes the city"


def test_the_page_reads_one_row_more_than_it_shows():
    calls = []
    d = fake(calls)
    call(d, "list_drives", {"limit": 5, "offset": 10})
    name, args = calls[-1]
    assert name == "drives" and args[1] == 16, "offset + limit + 1: the extra row says whether there is a next page"


def test_charges_and_the_one_charge_and_drive_in_full():
    d = fake()
    err, out = call(d, "list_charges", {"period": "last_7_days"})
    assert (
        not err
        and out["currency"] == "$"
        and out["rows"] == [{"id": 3, "car": "Alpha", "start": "2026-10-11 09:30", "minutes": 120, "where": "Home", "kind": "AC", "battery": [40, 80], "kwh": 28.0, "cost": 3.51, "home": True}]
    )
    err, full = call(d, "get_charge", {"id": 3})
    assert not err and full["end"] == "2026-10-11 11:30" and full["range_added_mi"] == 120 and full["plan"] == "Springfield EV" and full["solar_kwh"] == 10.0 and "at" not in full
    err, full = call(d, "get_drive", {"id": 7})
    assert not err and full["top_mph"] == 61 and full["weather"] == "light rain" and full["highest_ft"] == 656 and full["odometer"] == [1000, 1012] and full["cost_is_estimate"] is True
    assert call(d, "get_drive", {"id": 99}) == (True, "no drive 99") and call(d, "get_charge", {"id": 99}) == (True, "no charge 99")


def test_stats_add_up_the_period_and_say_what_is_estimated():
    d = fake()
    err, s = call(d, "stats", {"period": "last_month"})
    assert not err and s["period"] == "last_month" and s["drives"] == 20 and s["miles"] == 150.0 and s["charged_kwh"] == 80.0 and s["charging_cost"] == 18.0
    assert s["wh_per_mile"] == 400 and s["miles_per_kwh"] == 2.5 and s["charging_cost_includes_estimates"] is True and s["charges_without_a_price"] == 2 and "buckets" not in s
    err, s = call(d, "stats", {"by": "month"})
    assert s["period"] == "this_month", "a month when none is asked"
    assert [b["from"] for b in s["buckets"]] == ["2026-09-01", "2026-10-01"] and s["buckets"][1] == {"from": "2026-10-01", "drives": 10, "miles": 50.0, "charged_kwh": 40.0, "charging_cost": 6.0}


def test_stats_ask_the_database_for_the_period_asked():
    calls = []
    call(fake(calls), "stats", {"period": "2026-03", "car": "Bravo"})
    vin, by, lo, hi = calls[-1][1]
    assert vin == BRAVO and by == "month" and lo == datetime(2026, 3, 1, tzinfo=TZ) and hi == datetime(2026, 4, 1, tzinfo=TZ)


def test_battery_health_is_the_lifetime_report_in_a_few_figures():
    d = fake()
    err, out = call(d, "battery_health")
    alpha, bravo = out
    assert not err and alpha == {
        "car": "Alpha",
        "range_at_100pct_mi_now": 301,
        "first_mi": 310,
        "best_mi": 312,
        "charges_used": 41,
        "from": "2025-01-05",
        "to": "2026-10-10",
        "odometer_mi": [900, 12000],
        "lost_since_best": {"mi": 11, "pct": 3.5},
        "lost_since_first": {"mi": 9, "pct": 2.8},
    }
    assert "not enough charges" in bravo["note"]


def test_records_routes_places_and_software():
    d = fake()
    err, rec = call(d, "records", {"period": "2026"})
    assert (
        not err
        and rec["period"] == "2026"
        and rec["groups"][0]["records"] == [{"record": "Longest drive", "value": 12.3, "unit": "mi"}, {"record": "Most efficient", "value": 280, "unit": "Wh/mi", "among": "drives of 10 mi or more"}]
    )
    assert rec["groups"][0]["from"] == "Home" and rec["groups"][1]["where"] == "Home" and rec["groups"][1]["records"][0]["unit"] == "kW"
    err, routes = call(d, "top_routes", {"span": "year"})
    assert routes["routes"][0] == {"route": "Home ⇄ Work", "drives": 40, "typical_minutes": 25, "wh_per_mile": 270, "last": "2026-10-13", "tag": "Commute"}
    assert routes["routes"][1]["route"] == "Home ⇄ an unnamed spot"
    err, places = call(d, "top_places")
    assert places["places"] == [{"place": "Home", "arrivals": 80, "last": "2026-10-13"}, {"place": "an unnamed spot", "arrivals": 9, "last": "2026-10-13"}]
    err, sw = call(d, "software_versions", {"car": "Alpha", "limit": 1})
    assert sw == [
        {
            "car": "Alpha",
            "updates_every_days": 22,
            "days_since_last_update": 4,
            "versions": [{"version": "2026.8.1", "installed": "2026-10-09 09:30", "days": 4, "odometer_mi": 11900, "range_at_100pct_mi": 301}],
        }
    ]


def test_routes_look_back_the_span_asked():
    calls = []
    call(fake(calls), "top_routes", {"span": "90d", "car": "Alpha"})
    assert calls[-1] == ("routes", (ALPHA, 90))
    call(fake(calls), "top_places")
    assert calls[-1] == ("routes", (None, None))


# ---- who sees what ------------------------------------------------------------------------------


def test_no_answer_carries_a_coordinate_unless_the_token_is_scoped_for_them():
    d = fake()
    plain, with_coords = Scope(), Scope(coords=True)
    reads = [
        ("current_status", {}),
        ("list_drives", {"limit": 1}),
        ("list_charges", {}),
        ("get_drive", {"id": 7}),
        ("get_charge", {"id": 3}),
        ("records", {}),
        ("top_routes", {}),
        ("top_places", {}),
        ("stats", {}),
        ("battery_health", {}),
        ("software_versions", {}),
    ]
    for name, args in reads:
        text = json.dumps(call(d, name, args, plain)[1])
        for leak in ("39.78", "-89.65", '"lat"', '"lon"', "address", "Elm Street", "speed_mph", "heading", '"at"', "start_at", "end_at"):
            assert leak not in text, (name, leak)
    st = call(d, "current_status", {"car": "Alpha"}, with_coords)[1][0]
    assert st["lat"] == 39.7817 and st["lon"] == -89.6501 and st["address"] == "1 Elm St, Springfield"
    row = call(d, "list_drives", {"limit": 1}, with_coords)[1]["rows"][0]
    assert row["start_at"] == [39.7817, -89.6501] and row["to"] == "12 Elm Street, Springfield"
    assert call(d, "get_charge", {"id": 3}, with_coords)[1]["at"] == [39.7817, -89.6501]
    assert call(d, "top_places", {}, with_coords)[1]["places"][1]["at"] == [39.8, -89.65]


@pytest.mark.parametrize("tool,args", [("current_status", {}), ("battery_health", {}), ("software_versions", {}), ("list_cars", {})])
def test_a_token_for_one_car_sees_that_car_only(tool, args):
    err, out = call(fake(), tool, args, Scope(vin=BRAVO))
    assert not err and [c.get("car") or c["name"] for c in out] == ["Bravo"]


def test_a_token_for_one_car_cannot_ask_for_another_or_the_garage():
    calls = []
    d = fake(calls)
    err, text = call(d, "current_status", {"car": "Alpha"}, Scope(vin=BRAVO))
    assert err and text == call(d, "current_status", {"car": "Zeta"}, Scope(vin=BRAVO))[1].replace("Zeta", "Alpha") and calls == [], "no tell between a car that is not there and one that is not yours"
    assert "Bravo" in text and "Alpha, " not in text
    for tool, args in (("list_drives", {}), ("list_charges", {}), ("stats", {}), ("records", {}), ("top_routes", {}), ("top_places", {})):
        calls.clear()
        call(d, tool, args, Scope(vin=BRAVO))
        assert calls and all(a[0] == BRAVO for _, a in calls), (tool, calls)
    calls.clear()
    call(d, "get_drive", {"id": 7}, Scope(vin=BRAVO))
    assert calls[-1] == ("drive", (BRAVO, 7)), "the lookup is made inside the token's car, so another car's id finds nothing"


# ---- periods ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period,lo,hi",
    [
        ("today", "2026-10-13", None),
        ("yesterday", "2026-10-12", "2026-10-13"),
        ("this_week", "2026-10-12", None),
        ("last_week", "2026-10-05", "2026-10-12"),
        ("this_month", "2026-10-01", None),
        ("last_month", "2026-09-01", "2026-10-01"),
        ("this_year", "2026-01-01", None),
        ("last_year", "2025-01-01", "2026-01-01"),
        ("2025-12", "2025-12-01", "2026-01-01"),
        ("2024", "2024-01-01", "2025-01-01"),
    ],
)
def test_named_periods_are_local_midnights(period, lo, hi):
    a, b, _ = mcptools.period_window(period, None, None, NOW, TZ)
    assert a == datetime.fromisoformat(lo).replace(tzinfo=TZ) and b == (datetime.fromisoformat(hi).replace(tzinfo=TZ) if hi else None)


def test_dates_given_directly_and_the_ways_they_go_wrong():
    lo, hi, label = mcptools.period_window(None, "2026-03-01", "2026-03-31", NOW, TZ)
    assert lo == datetime(2026, 3, 1, tzinfo=TZ) and hi == datetime(2026, 4, 1, tzinfo=TZ), "a bare end date takes in that day"
    assert mcptools.period_window(None, None, "2026-03-31T18:00", NOW, TZ)[1] == datetime(2026, 3, 31, 18, tzinfo=TZ)
    assert mcptools.period_window("all", None, None, NOW, TZ)[:2] == (None, None)
    assert mcptools.period_window("last_7_days", None, None, NOW, TZ)[0] == NOW - timedelta(days=7)
    for bad in (
        dict(period="fortnight"),
        dict(period="2026-13"),
        dict(since="march"),
        dict(since="2026-03-02", until="2026-03-01"),
        dict(period="0000"),
        dict(period="0000-05"),
        dict(period="9999-05"),
        dict(period="9999"),
        dict(period="1999-12"),
        dict(until="9999-12-31"),
        dict(since="0001-01-01T00:00+14:00"),
        dict(since="1990-01-01"),
    ):
        with pytest.raises(mcpserver.ToolError):
            mcptools.period_window(bad.get("period"), bad.get("since"), bad.get("until"), NOW, TZ)


# ---- through the site ---------------------------------------------------------------------------

TOKEN, READ, STATUS = "mcp-token-springfield-alpha", "read-token-springfield-bravo", "status-token-springfield"


@pytest.fixture
def site(monkeypatch):
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")
    import auth
    from fastapi.testclient import TestClient

    monkeypatch.setitem(app.AUTH, "enabled", True)
    monkeypatch.setitem(app.AUTH, "secret", auth.new_secret())
    monkeypatch.setitem(app.AUTH, "password", auth.hash_password("a good long password", n=2**10))
    monkeypatch.setattr(app, "TOKENS", {})
    for raw, kind, vin, coords in ((TOKEN, "mcp", None, False), ("mcp-coords", "mcp", None, True), ("mcp-bravo", "mcp", BRAVO, False), (READ, "read", None, False), (STATUS, "status", None, False)):
        app.TOKENS[auth.token_hash(raw)] = {"label": kind, "vin": vin, "kind": kind, "last4": raw[-4:], "coords": coords}
    monkeypatch.setattr(app, "mcp_data", lambda: fake())
    app.audited = []
    monkeypatch.setattr(app, "audit", lambda token, method, path, status, ip, held=0: app.audited.append((token["label"], method, path, status)))
    monkeypatch.setattr(app, "PUBLIC_URL", "https://garage.example.test")
    return TestClient(app.server, follow_redirects=False, client=("192.168.1.40", 50000))


def post(client, body, token=TOKEN, **headers):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/mcp", content=json.dumps(body), headers={**h, "Content-Type": "application/json", **headers})


def tool_call(client, name, args=None, token=TOKEN):
    r = post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}, token)
    assert r.status_code == 200
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_the_handshake_and_a_tool_over_http(site):
    r = post(site, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "car-command" and "Today is Tuesday 2026-10-13" in r.json()["result"]["instructions"]
    assert post(site, {"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert [c["name"] for c in tool_call(site, "list_cars")] == ["Alpha", "Bravo"]


def test_the_scope_rides_in_the_token(site):
    assert "lat" not in tool_call(site, "current_status", {"car": "Alpha"})[0]
    assert tool_call(site, "current_status", {"car": "Alpha"}, "mcp-coords")[0]["lat"] == 39.7817
    assert [c["name"] for c in tool_call(site, "list_cars", token="mcp-bravo")] == ["Bravo"]


def test_no_token_a_bad_token_or_another_kind_is_refused(site):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = post(site, ping, token=None)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer" and "MCP token" in r.json()["error"]
    assert post(site, ping, token="not-a-token").status_code == 401
    for other in (READ, STATUS):
        r = post(site, ping, token=other)
        assert r.status_code == 403 and "MCP token" in r.json()["error"], other


def test_a_signed_in_browser_cannot_use_it_and_an_mcp_token_opens_nothing_else(site):
    assert site.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/api/now", "/api/whoami", "/api/drives", "/drives", "/settings/access", "/api/admin/tesla"):
        r = site.get(path, headers=h)
        assert r.status_code in (303, 403, 401), (path, r.status_code)
        assert r.status_code != 200
    assert site.get("/api/now", headers=h).status_code == 403 and "only /mcp" in site.get("/api/now", headers=h).json()["error"]


def test_it_is_post_only_and_refuses_a_web_page(site):
    h = {"Authorization": f"Bearer {TOKEN}"}
    for verb in ("get", "delete"):
        r = getattr(site, verb)("/mcp", headers=h)
        assert r.status_code == 405 and r.headers["allow"] == "POST"
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert post(site, ping, Origin="https://evil.example").status_code == 403
    assert post(site, ping, Origin="http://192.168.1.40:3044").status_code == 200, "a page on the home network is fine"
    assert post(site, ping, Origin="http://mini.local:3044").status_code == 200
    assert post(site, ping, Origin="https://garage.example.test").status_code == 200, "the configured public address"
    assert post(site, ping, Origin="https://other.example.test").status_code == 403, "not configured"
    # a rebinding page names itself in both Origin and Host: the match proves nothing, the missing bearer is what stops it
    assert post(site, ping, Origin="http://evil.test", Host="evil.test").status_code == 403
    assert site.post("/mcp", json=ping, headers={"Origin": "http://evil.test", "Host": "evil.test"}).status_code == 401
    assert post(site, {"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "x" * 70000}).status_code == 413
    assert site.post("/mcp", content="{nope", headers={"Authorization": f"Bearer {TOKEN}"}).json()["error"]["code"] == mcpserver.PARSE_ERROR


def test_a_token_that_is_revoked_stops_at_once(site):
    import auth

    import app

    del app.TOKENS[auth.token_hash(TOKEN)]
    assert post(site, {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_the_mcp_token_kind_is_known_and_never_a_surprise():
    import auth

    assert auth.kind_of("mcp") == "mcp" and auth.kind_of("MCP ") == "read" and auth.kind_of("mcp; admin") == "read"
    assert "mcp" in auth.KINDS and auth.MCP_PATH == "/mcp"
    assert "coords BOOLEAN NOT NULL DEFAULT false" in auth.API_DDL, "tokens made before it have no coordinates"


def test_the_token_page_offers_an_mcp_token_and_shows_what_to_paste():
    from types import SimpleNamespace

    import jinja2

    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    base = dict(
        now=NOW,
        root="/cc",
        q="",
        car={"vin": ALPHA, "name": "Alpha"},
        vehicles=[],
        colors={},
        active="settings-access",
        label="Sign-in and API",
        st={},
        conv={},
        auth_enabled=True,
        saved=False,
        section="access",
    )
    made = NOW
    html = env.get_template("settings_access.html").render(
        **base,
        tokens=[{"hash": "ab" * 32, "label": "assistant", "kind": "mcp", "coords": True, "last4": "zz99", "car": "Alpha", "created_at": made, "last_used_at": None}],
        minted="raw-token-shown-once",
        minted_label="assistant",
        minted_kind="mcp",
        minted_coords=True,
        request=SimpleNamespace(url=SimpleNamespace(scheme="https", netloc="example.test")),
    )
    assert 'value="mcp"' in html and 'name="coords"' in html and 'class="set-row mcp-only"' in html
    assert '<span class="badge kind">MCP</span>' in html and "with coordinates" in html
    assert "https://example.test/cc/mcp" in html and "claude mcp add --transport http car-command https://example.test/cc/mcp" in html and "Bearer raw-token-shown-once" in html
    assert "Your MCP token" in html


# ---- the two eras -------------------------------------------------------------------------------

META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientInfo": {"name": "c", "version": "1"}, "io.modelcontextprotocol/clientCapabilities": {}}


def modern(method, params=None, headers=None, version="2026-07-28"):
    """One modern request: the version in _meta, and the headers a modern client mirrors from the body."""
    p = {**(params or {}), "_meta": {**META, "io.modelcontextprotocol/protocolVersion": version}}
    h = {"mcp-protocol-version": version, "mcp-method": method, **({"mcp-name": p["name"]} if method == "tools/call" else {}), **(headers or {})}
    return rpc({"jsonrpc": "2.0", "id": 1, "method": method, "params": p}, headers={k: v for k, v in h.items() if v is not None}, status=True)


def test_server_discover_names_what_is_spoken_and_what_is_offered():
    code, body = modern("server/discover")
    res = body["result"]
    assert code == 200 and res["resultType"] == "complete" and res["supportedVersions"][0] == "2026-07-28" and "2025-11-25" in res["supportedVersions"]
    assert res["capabilities"] == {"tools": {}} and res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "car-command" and res["instructions"] == "hello"
    assert res["ttlMs"] == 0 and res["cacheScope"] == "private", "an answer that depends on the token is not shared"


def test_a_modern_request_needs_no_initialize_and_its_results_say_complete():
    code, body = modern("tools/list")
    assert code == 200 and body["result"]["resultType"] == "complete" and len(body["result"]["tools"]) == 12
    code, body = modern("tools/call", {"name": "list_cars", "arguments": {}})
    assert code == 200 and body["result"]["resultType"] == "complete" and json.loads(body["result"]["content"][0]["text"]) == [{"name": "Alpha"}, {"name": "Bravo"}]


def test_a_modern_request_with_an_unknown_version_is_400_and_names_the_ones_spoken():
    code, body = modern("tools/list", version="2099-01-01")
    assert code == 400 and body["error"]["code"] == mcpserver.UNSUPPORTED_VERSION
    assert body["error"]["data"]["supported"][0] == "2026-07-28" and body["error"]["data"]["requested"] == "2099-01-01"
    code, body = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"mcp-protocol-version": "1900-01-01"}, status=True)
    assert code == 400 and body["error"]["code"] == mcpserver.UNSUPPORTED_VERSION, "an unknown version in the header alone"


@pytest.mark.parametrize(
    "headers",
    [
        {"mcp-protocol-version": None},
        {"mcp-protocol-version": "2025-11-25"},
        {"mcp-method": None},
        {"mcp-method": "tools/list"},
    ],
)
def test_headers_that_do_not_match_the_body_are_a_400_header_mismatch(headers):
    code, body = modern("tools/call", {"name": "list_cars"}, headers)
    assert code == 400 and body["error"]["code"] == mcpserver.HEADER_MISMATCH


def test_the_tool_name_header_must_match_and_may_be_base64():
    assert modern("tools/call", {"name": "list_cars"}, {"mcp-name": "get_drive"})[1]["error"]["code"] == mcpserver.HEADER_MISMATCH
    assert modern("tools/call", {"name": "list_cars"}, {"mcp-name": None})[1]["error"]["code"] == mcpserver.HEADER_MISMATCH
    import base64

    sentinel = "=?base64?" + base64.b64encode(b"list_cars").decode() + "?="
    assert modern("tools/call", {"name": "list_cars"}, {"mcp-name": sentinel})[0] == 200
    assert modern("tools/call", {"name": "list_cars"}, {"mcp-name": "=?base64?!!!?="})[0] == 400


def test_a_modern_method_we_do_not_have_is_404_with_the_method_not_found_body():
    code, body = modern("resources/list")
    assert code == 404 and body["error"]["code"] == mcpserver.NO_METHOD
    assert modern("ping")[0] == 404, "ping left the protocol in 2026-07-28"


def test_a_modern_header_without_the_meta_is_a_mismatch_not_a_legacy_request():
    code, body = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"mcp-protocol-version": "2026-07-28"}, status=True)
    assert code == 400 and body["error"]["code"] == mcpserver.HEADER_MISMATCH


def test_the_legacy_era_still_works_with_or_without_its_version_header():
    for h in ({}, {"mcp-protocol-version": "2025-11-25"}, {"mcp-protocol-version": "2025-03-26"}):
        code, body = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h, status=True)
        assert code == 200 and "resultType" not in body["result"] and len(body["result"]["tools"]) == 12
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})["result"]["protocolVersion"] == "2025-11-25"
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, status=True)[0] == 200, "an unknown legacy method is a JSON-RPC error on a 200, as before"


def test_a_required_argument_is_in_the_schema_and_asked_for_in_words():
    tools = {t["name"]: t for t in rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert tools["get_drive"]["inputSchema"]["required"] == ["id"] and tools["get_charge"]["inputSchema"]["required"] == ["id"]
    assert "required" not in tools["stats"]["inputSchema"]
    d = fake()
    assert call(d, "get_drive", {}) == (True, "id is required") and call(d, "get_charge", {"id": None}) == (True, "id is required")


def test_a_typed_vin_still_finds_the_car_that_list_cars_does_not_print():
    err, out = call(fake(), "current_status", {"car": BRAVO})
    assert not err and out[0]["car"] == "Bravo"
    assert "Bravo" not in call(fake(), "current_status", {"car": ALPHA, "nope": 1})[1]


def test_records_and_places_with_a_street_name_give_the_city_without_coordinates():
    d = fake()
    d.records = lambda vin, lo, hi: [{"kind": "drive", "row": drive_row(named_end=False), "titles": [{"key": "distance", "label": "Longest drive", "value": 5.0, "fmt": "dist", "note": None}]}]
    text = json.dumps(call(d, "records")[1])
    assert "Elm Street" not in text and '"to":"Springfield"' in text.replace(" ", "")
    assert "12 Elm Street" in json.dumps(call(d, "records", {}, Scope(coords=True))[1])


def test_a_nested_body_is_a_400_not_a_crash(site):
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    r = site.post("/mcp", content="[" * 3000 + "]" * 3000, headers=h)
    assert r.status_code == 400
    assert site.post("/mcp", content=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]), headers=h).status_code == 400


def test_over_http_the_modern_handshake_and_a_mismatch(site):
    h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"}
    r = post(site, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": META}}, **h)
    assert r.status_code == 200 and r.json()["result"]["supportedVersions"][0] == "2026-07-28"
    r = post(site, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}, **{"MCP-Protocol-Version": "2026-07-28"})
    assert r.status_code == 400 and r.json()["error"]["code"] == mcpserver.HEADER_MISMATCH
    assert post(site, {"jsonrpc": "2.0", "id": 1, "method": "nope", "params": {"_meta": META}}, **{**h, "Mcp-Method": "nope"}).status_code == 404


def test_each_tool_call_leaves_a_row_with_the_tool_and_the_token_and_never_the_arguments(site):
    import app

    tool_call(site, "list_drives", {"period": "yesterday", "car": "Alpha"})
    post(site, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    post(site, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_drive", "arguments": {}}})
    assert app.audited == [("mcp", "MCP", "list_drives", 200), ("mcp", "MCP", "get_drive", 200)], "a list and a ping are not questions"
    assert "yesterday" not in str(app.audited) and "Alpha" not in str(app.audited)


def test_other_tokens_on_the_command_route_and_cloudflare_identity_alone(site):
    import auth

    import app

    for kind in ("control", "admin"):
        raw = f"{kind}-springfield"
        app.TOKENS[auth.token_hash(raw)] = {"label": kind, "vin": None, "kind": kind, "last4": raw[-4:]}
        assert post(site, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=raw).status_code == 403
    r = site.post(f"/api/cars/{ALPHA}/command", json={"command": "lock"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    assert site.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Cf-Access-Authenticated-User-Email": "a@example.test"}).status_code == 401


def test_the_token_page_has_the_labelled_snippets_and_the_log():
    from types import SimpleNamespace

    import jinja2

    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    base = dict(
        now=NOW,
        root="/cc",
        q="",
        car={"vin": ALPHA, "name": "Alpha"},
        vehicles=[],
        colors={},
        active="settings-access",
        label="Sign-in and API",
        st={},
        conv={},
        auth_enabled=True,
        saved=False,
        section="access",
        tokens=[],
    )
    html = env.get_template("settings_access.html").render(
        **base,
        minted="raw-token-shown-once",
        minted_label="assistant",
        minted_kind="mcp",
        public_url="https://garage.example.test/carcommand",
        mcp_log=[{"at": NOW, "label": "assistant", "last4": "zz99", "tool": "list_drives"}],
        request=SimpleNamespace(url=SimpleNamespace(scheme="http", netloc="10.0.0.5:3044")),
    )
    assert "https://garage.example.test/carcommand/mcp" in html and "10.0.0.5" not in html, "the configured public address, not the one the page was opened on"
    assert "export CC_TOKEN=raw-token-shown-once" in html and 'Bearer $CC_TOKEN' in html
    assert "Claude Desktop" in html and "mcp-remote" in html and "Authorization:${CC_AUTH}" in html and '"env": {"CC_AUTH": "Bearer raw-token-shown-once"}' in html
    assert ".mcp.json, Cursor" in html and '"type": "http"' in html and "service token" in html
    assert "street addresses, latitude and longitude, speed and heading" in html
    assert "Recent MCP questions" in html and "list_drives" in html

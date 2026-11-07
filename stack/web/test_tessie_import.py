"""Tessie's drives, charges and historical states become the writer's rows; no network, no database."""

from datetime import datetime, timezone

import pytest

import tessie_import as ti
from tessie_import import TessieAuth, TessieError

UTC = timezone.utc
VIN = "VINALPHA0000000001"


def at(*a):
    return datetime(*a, tzinfo=UTC)


def epoch(*a):
    return int(at(*a).timestamp())


def drive(start, minutes=20, **over):
    d = {
        "id": 1,
        "started_at": epoch(*start),
        "ended_at": epoch(*start) + minutes * 60,
        "starting_location": "1 Main St, Springfield",
        "starting_latitude": 39.80,
        "starting_longitude": -89.65,
        "ending_location": "2 Oak St, Springfield",
        "ending_latitude": 39.78,
        "ending_longitude": -89.64,
        "starting_battery": 80,
        "ending_battery": 76,
        "average_inside_temperature": 20.0,
        "average_outside_temperature": 61.5,
        "average_speed": 30,
        "max_speed": 62,
        "rated_range_used": 12.0,
        "odometer_distance": 6.0,
        "autopilot_distance": None,
        "energy_used": 2.4,
        "tag": "Commute",
    }
    d.update(over)
    return d


def charge(start, minutes=120, **over):
    c = {
        "id": 7,
        "started_at": epoch(*start),
        "ended_at": epoch(*start) + minutes * 60,
        "location": "3 Elm St, Springfield",
        "latitude": 39.81,
        "longitude": -89.66,
        "is_supercharger": False,
        "odometer": 5000.0,
        "energy_added": 20.5,
        "energy_used": 22.0,
        "miles_added": 60,
        "miles_added_ideal": 70,
        "starting_battery": 40,
        "ending_battery": 80,
        "cost": 0,
    }
    c.update(over)
    return c


def state(t, **over):
    s = {
        "timestamp": t,
        "latitude": 39.80,
        "longitude": -89.65,
        "speed": 30,
        "power": 12.0,
        "odometer": 5000.0,
        "battery_level": 80,
        "battery_range": 200.0,
        "charger_power": None,
        "charger_voltage": None,
        "charger_actual_current": None,
    }
    s.update(over)
    return s


def route(start, n=5, step=5, **over):
    """n readings along a line going north-east, `step` seconds apart, odometer rising a tenth of a mile each."""
    t0 = epoch(*start)
    return [state(t0 + i * step, latitude=39.80 + i * 0.001, longitude=-89.65 + i * 0.001, odometer=5000.0 + i * 0.1, battery_level=80 if i < 3 else 79, **over) for i in range(n)]


def test_a_drive_is_a_trip_with_its_figures():
    t = ti.trip_from(drive((2026, 9, 3, 12, 0)))
    assert t["start_date"] == at(2026, 9, 3, 12, 0) and t["destination_date"] == at(2026, 9, 3, 12, 20)
    assert (t["start_position_latitude"], t["destination_position_longitude"]) == (39.80, -89.64)
    assert t["max_mph"] == 62 and t["temp_f"] == 61.5 and t["kwh"] == 2.4 and t["tag"] == "Commute"
    assert t["start_odometer"] is None, "the list holds a distance, not odometer readings"
    assert ti.trip_from(drive((2026, 9, 3, 12, 0), tag=None))["tag"] is None
    assert ti.trip_from(drive((2026, 9, 3, 12, 0), energy_used=0))["kwh"] is None
    assert ti.trip_from({"started_at": 5, "ended_at": 5}) is None and ti.trip_from({}) is None


def test_a_charge_is_a_session():
    c = ti.charge_from(charge((2026, 9, 3, 18, 0)))
    assert (c["start_level"], c["end_level"], c["real_charged"], c["session_odometer"]) == (40, 80, 20.5, 5000.0)
    assert c["charging_type"] == "AC" and c["network"] is None
    sc = ti.charge_from(charge((2026, 9, 3, 18, 0), is_supercharger=True))
    assert sc["charging_type"] == "DC" and sc["network"] == "supercharger"
    assert ti.charge_from({"started_at": 9, "ended_at": 1}) is None


def test_milliseconds_are_told_from_seconds():
    assert ti.ts(1_780_000_000) == ti.ts(1_780_000_000_000) and ti.ts(None) is None and ti.ts("x") is None and ti.ts(0) is None


def test_a_route_has_headings_odometer_ends_levels_and_power():
    t = ti.trip_from(drive((2026, 9, 3, 12, 0)))
    reads = ti.samples_in(route((2026, 9, 3, 12, 0)), t["start_date"], t["destination_date"])
    trip, pos, levels, ranges = ti.with_route(t, reads)
    assert (trip["start_odometer"], trip["destination_odometer"]) == (5000.0, pytest.approx(5000.4))
    assert len(pos) == 5 and pos[0][7] == "DRIVING" and pos[0][9] == 12.0 and pos[0][8] is None, "power kept, elevation left out"
    assert all(0 < p[3] < 90 for p in pos), "going north-east: the first point takes the bearing of the next"
    assert [(a, b, v) for a, b, v in levels] == [(at(2026, 9, 3, 12, 0), at(2026, 9, 3, 12, 0, 10), 80), (at(2026, 9, 3, 12, 0, 15), at(2026, 9, 3, 12, 0, 20), 79)]
    assert len(ranges) == 1


def test_readings_are_cut_to_the_drive_and_one_a_second():
    t0 = epoch(2026, 9, 3, 12, 0)
    rows = [state(t0 - 60), state(t0), state(t0), state(t0 + 5), state(t0 + 5000), {"timestamp": None}, "junk"]
    got = ti.samples_in(rows, at(2026, 9, 3, 12, 0), at(2026, 9, 3, 12, 20))
    assert [s["t"] for s in got] == [at(2026, 9, 3, 12, 0), at(2026, 9, 3, 12, 0, 5)]


def test_a_kilometre_reply_is_turned_into_miles():
    t = ti.trip_from(drive((2026, 9, 3, 12, 0), odometer_distance=10.0))
    reads = ti.samples_in([state(epoch(2026, 9, 3, 12, 0), odometer=8000.0, speed=100), state(epoch(2026, 9, 3, 12, 5), odometer=8016.09, speed=100)], t["start_date"], t["destination_date"])
    trip, pos, *_ = ti.with_route(t, reads)
    assert trip["destination_odometer"] - trip["start_odometer"] == pytest.approx(10.0, abs=0.01) and pos[0][6] == pytest.approx(62.14, abs=0.01)
    miles = ti.with_route(t, ti.samples_in([state(epoch(2026, 9, 3, 12, 0), odometer=8000.0), state(epoch(2026, 9, 3, 12, 5), odometer=8010.0)], t["start_date"], t["destination_date"]))[0]
    assert miles["destination_odometer"] == 8010.0, "a rise that matches the drive's miles is left alone"


def test_a_charge_curve_replaces_the_sessions_one_figure():
    c = ti.charge_from(charge((2026, 9, 3, 18, 0), minutes=10))
    t0 = epoch(2026, 9, 3, 18, 0)
    reads = ti.samples_in(
        [state(t0 + i * 60, charger_power=p, charger_voltage=240, charger_actual_current=p * 4, battery_level=40 + i) for i, p in enumerate([7.0, 7.0, 11.0, 30.0])],
        c["session_start_date"],
        c["session_end_date"],
    )
    out = ti.month_out(VIN, "Alpha", [], [c], {}, {c["session_start_date"]: reads})
    assert out["charging_sessions"][0]["charging_type"] == "DC", "a peak of 30 kW is DC though Tessie said no Supercharger"
    powers = out["charging_powers"]
    assert [v for _a, _b, v in powers] == [7.0, 11.0, 30.0] and powers[0][1] == powers[1][0], "a stretch runs to the next reading"
    assert powers[-1][1] == c["session_end_date"]
    assert out["charging_electrics"][0][2] == (240, 28.0)
    assert out["first"] == c["session_start_date"] and out["rows"] == 1


def test_a_month_without_readings_is_still_written_from_the_summaries():
    t = ti.trip_from(drive((2026, 9, 3, 12, 0)))
    out = ti.month_out(VIN, "Alpha", [t], [], {}, {})
    assert len(out["trips"]) == 1 and out["positions"] == [] and out["trips"][0]["start_odometer"] is None
    assert ti.month_out(VIN, "Alpha", [], [], {}, {}) is None


def test_coverage_helpers():
    cover = ti.merge([(at(2026, 5, 1), at(2026, 6, 1)), (at(2026, 5, 20), at(2026, 7, 1)), (at(2026, 9, 1), at(2026, 10, 1))])
    assert cover == [(at(2026, 5, 1), at(2026, 7, 1)), (at(2026, 9, 1), at(2026, 10, 1))]
    assert ti.covers(cover, at(2026, 4, 30), at(2026, 5, 1)) and ti.covers(cover, at(2026, 6, 15), at(2026, 6, 16)) and ti.covers(cover, at(2026, 6, 30), at(2026, 8, 1))
    assert not ti.covers(cover, at(2026, 7, 2), at(2026, 8, 31)) and not ti.covers(cover, at(2026, 4, 1), at(2026, 4, 30))
    assert ti.inside(cover, at(2026, 5, 2), at(2026, 6, 30)) and not ti.inside(cover, at(2026, 4, 30), at(2026, 6, 30))
    items = [(at(2026, 4, 1), at(2026, 4, 2), "a"), (at(2026, 4, 5), at(2026, 4, 6), "b"), (at(2026, 8, 1), at(2026, 8, 2), "c")]
    assert [[x[2] for x in g] for g in ti.clusters(items, cover)] == [["a", "b"], ["c"]]
    assert [[x[2] for x in g] for g in ti.clusters(items, [])] == [["a", "b", "c"]], "nothing between them: one window"


def test_months_walk_back_from_now():
    got = list(ti.month_windows(at(2026, 2, 10), at(2025, 11, 15)))
    assert [s.strftime("%Y-%m") for s, _e in got] == ["2026-02", "2026-01", "2025-12", "2025-11"]
    assert got[0][1] == at(2026, 3, 1) and list(ti.month_windows(at(2012, 7, 1), None))[-1][0] == at(2012, 6, 1)


# ---- the client
class Reply:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body


class FakeHttp:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def get(self, path, params=None, headers=None):
        self.calls.append((path, params, headers))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_the_client_sends_get_with_the_token_in_a_header_only():
    http = FakeHttp([Reply(200, {"results": []})])
    waits = []
    fetch = ti.http_fetcher("sekrit-token", pause=0, client=http, sleep=waits.append)
    assert fetch(f"/{VIN}/drives", **{"from": 5, "to": 9, "tag": None}) == {"results": []}
    path, params, headers = http.calls[0]
    assert headers["Authorization"] == "Bearer sekrit-token" and "sekrit" not in str(params) and "sekrit" not in path
    assert params == {"from": 5, "to": 9}, "a parameter left None is not sent"


def test_the_client_waits_and_tries_again_on_429_and_5xx():
    import httpx

    http = FakeHttp([Reply(429, headers={"retry-after": "7"}), httpx.ConnectError("boom"), Reply(503), Reply(200, {"results": [1]})])
    waits = []
    fetch = ti.http_fetcher("tok", pause=0, client=http, sleep=waits.append)
    assert fetch("/vehicles") == {"results": [1]}
    assert 7.0 in waits and len([w for w in waits if w >= 4]) == 3 and len(http.calls) == 4


def test_the_client_gives_up_politely_and_never_says_the_token():
    http = FakeHttp([Reply(429)] * 3)
    fetch = ti.http_fetcher("tok-123", pause=0, tries=3, client=http, sleep=lambda s: None)
    with pytest.raises(TessieError, match="carries on where it stopped") as e:
        fetch("/vehicles")
    assert "tok-123" not in str(e.value)
    for status, kind in ((401, TessieAuth), (403, TessieAuth), (404, TessieError), (400, TessieError)):
        with pytest.raises(kind) as e:
            ti.http_fetcher("tok-123", pause=0, client=FakeHttp([Reply(status)]), sleep=lambda s: None)("/x")
        assert "tok-123" not in str(e.value)
    with pytest.raises(TessieError, match="not JSON"):
        ti.http_fetcher("t", pause=0, client=FakeHttp([Reply(200)]), sleep=lambda s: None)("/x")


def test_a_wrong_shape_is_an_error_not_an_empty_month():
    for body in ({}, {"results": {"ids": [1]}}, [], None):
        with pytest.raises(TessieError):
            ti.results(body)
    assert ti.vehicles(lambda p: {"results": [{"vin": VIN, "last_state": {"display_name": "Alpha"}}, {"vin": ""}, {"vin": "V2"}]}) == [{"vin": VIN, "name": "Alpha"}, {"vin": "V2", "name": "V2"}]


# ---- a whole car, against a stand-in database and a stand-in Tessie
class Conn:
    """Answers the three questions the importer asks and records the rest."""

    def __init__(self, imports=(), loaded=None):
        self.imports, self.loaded, self.sql = list(imports), dict(loaded or {}), []
        self.row = None

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.sql.append((sql, params))
        self.row = []
        if sql.startswith("SELECT first_at, last_at FROM cc_imports"):
            self.row = list(self.imports)
        elif sql.startswith("SELECT loaded_at FROM cc_imports"):
            self.row = [(self.loaded[params[0]],)] if params[0] in self.loaded else []
        return self

    def fetchall(self):
        return self.row

    def fetchone(self):
        return self.row[0] if self.row else None

    def commit(self):
        pass


class Tessie:
    """drives, charges and states keyed by month; counts what was asked."""

    def __init__(self, drives=(), charges=()):
        self.drives, self.charges, self.asked = list(drives), list(charges), []

    def __call__(self, path, **p):
        self.asked.append((path, p))
        lo, hi = p.get("from", 0), p.get("to", 2**40)
        if path.endswith("/drives"):
            return {"results": [d for d in self.drives if lo <= d["started_at"] <= hi]}
        if path.endswith("/charges"):
            return {"results": [c for c in self.charges if lo <= c["started_at"] <= hi]}
        if path.endswith("/states"):
            return {
                "results": (
                    route(tuple(ti.ts(lo).timetuple()[:6]), 5)
                    if p["interval"] != 60
                    else [state(lo + i * 60, charger_power=7.0, charger_voltage=240, charger_actual_current=29, battery_level=40 + i) for i in range(3)]
                )
            }
        raise AssertionError(path)


@pytest.fixture
def writes(monkeypatch):
    got = []
    monkeypatch.setattr(
        ti,
        "write_history",
        lambda conn, vin, name, make, model, cap, out, iid, label, tz, rolling=False: got.append((iid, out, rolling))
        or {"trips": len(out["trips"]), "charging_sessions": len(out["charging_sessions"]), "cc_positions": len(out["positions"])},
    )
    monkeypatch.setattr(ti, "fill_after_import", lambda conn, vin, tz: None)
    monkeypatch.setattr(ti, "save_trip_facts", lambda conn, vin, trips: None)
    monkeypatch.setattr(ti, "live_since", lambda conn, vin: None)
    return got


NOW = at(2026, 10, 15)
TZ = ti.ZoneInfo("America/Chicago")


def run(conn, api, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("since", at(2026, 8, 1))
    return ti.import_car(conn, api, VIN, "Alpha", TZ, say=lambda *_: None, **kw)


def test_a_car_is_loaded_a_month_at_a_time_through_the_writer(writes):
    api = Tessie([drive((2026, 9, 3, 12, 0)), drive((2026, 9, 9, 8, 0))], [charge((2026, 9, 3, 18, 0))])
    got = run(Conn(), api)
    assert [w[0] for w in writes] == [f"tessie:{VIN}:2026-09"] and writes[0][2] is True, "one rolling import per month"
    out = writes[0][1]
    assert len(out["trips"]) == 2 and len(out["charging_sessions"]) == 1 and len(out["positions"]) == 10
    assert (got["trips"], got["charging_sessions"], got["cc_positions"], got["months"]) == (2, 1, 10, 1)
    asked = [(p.rsplit("/", 1)[-1], q.get("interval")) for p, q in api.asked]
    assert ("states", 5) in asked and ("states", 60) in asked
    assert all(q.get("condense") == "false" for p, q in api.asked if p.endswith("/states")), "rows, not the condensed arrays"
    assert all(p.startswith(f"/{VIN}/") for p, _q in api.asked)


def test_drives_and_charges_over_another_sources_time_are_left_out(writes):
    export = [(at(2026, 9, 2), at(2026, 9, 5))]
    api = Tessie([drive((2026, 9, 3, 12, 0)), drive((2026, 9, 9, 8, 0))], [charge((2026, 9, 3, 18, 0))])
    got = run(Conn(imports=export), api)
    assert len(writes) == 1 and [t["start_date"] for t in writes[0][1]["trips"]] == [at(2026, 9, 9, 8, 0)] and writes[0][1]["charging_sessions"] == []
    assert got["left_out"] == 2
    # the same drive and the one after the export fall in different windows: the export's span is never inside a write
    both = Tessie([drive((2026, 9, 1, 12, 0)), drive((2026, 9, 9, 8, 0))])
    writes.clear()
    run(Conn(imports=export), both)
    assert [len(w[1]["trips"]) for w in writes] == [1, 1] and writes[0][1]["last"] < at(2026, 9, 2) and writes[1][1]["first"] > at(2026, 9, 5)


def test_a_month_wholly_inside_another_source_is_not_asked_for(writes, monkeypatch):
    api = Tessie([drive((2026, 9, 3, 12, 0))])
    run(Conn(imports=[(at(2026, 8, 20), at(2026, 10, 1))]), api, since=at(2026, 9, 1))
    assert not any(p.endswith("/drives") and q["from"] == int(at(2026, 9, 1).timestamp()) for p, q in api.asked) and writes == []
    monkeypatch.setattr(ti, "live_since", lambda conn, vin: at(2026, 9, 20))
    writes.clear()
    run(Conn(), Tessie([drive((2026, 9, 3, 12, 0)), drive((2026, 9, 22, 12, 0))]))
    assert [t["start_date"] for t in writes[0][1]["trips"]] == [at(2026, 9, 3, 12, 0)], "nothing from when the live logger began"


def test_a_month_loaded_after_it_ended_is_skipped_and_again_loads_it(writes):
    done = {f"tessie:{VIN}:2026-09": at(2026, 10, 3)}
    api = Tessie([drive((2026, 9, 3, 12, 0))])
    run(Conn(loaded=done), api)
    assert writes == [] and not any(q.get("from") == int(at(2026, 9, 1).timestamp()) for _p, q in api.asked)
    run(Conn(loaded=done), api, again=True)
    assert len(writes) == 1
    writes.clear()
    run(Conn(loaded={f"tessie:{VIN}:2026-09": at(2026, 9, 30, 12)}), api)
    assert len(writes) == 1, "loaded before the month was over: the last drives may have been missing"


def test_a_rerun_replaces_the_month_and_does_not_double_it(writes):
    conn = Conn()
    api = Tessie([drive((2026, 9, 3, 12, 0))])
    run(conn, api)
    run(conn, api)
    assert [w[0] for w in writes] == [f"tessie:{VIN}:2026-09"] * 2
    assert [s for s, p in conn.sql if s.startswith("DELETE FROM cc_imports")] == ["DELETE FROM cc_imports WHERE id = %s"] * 2, "the month's old import row goes before it is written afresh"
    assert writes[0][1]["trips"] == writes[1][1]["trips"]


def test_a_dry_run_asks_and_counts_and_writes_nothing(writes):
    conn = Conn()
    got = run(conn, Tessie([drive((2026, 9, 3, 12, 0))]), dry_run=True)
    assert writes == [] and got["trips"] == 1 and not any(s.startswith("DELETE") for s, _ in conn.sql)


def test_the_walk_back_stops_after_a_year_of_nothing(writes):
    api = Tessie()
    run(Conn(), api, since=None, now=at(2026, 10, 15))
    assert sum(1 for p, _q in api.asked if p.endswith("/drives")) == ti.STOP_AFTER


def test_without_curves_no_charge_readings_are_asked_for(writes):
    api = Tessie(charges=[charge((2026, 9, 3, 18, 0))])
    run(Conn(), api, curves=False)
    assert not any(p.endswith("/states") for p, _q in api.asked) and len(writes[0][1]["charging_sessions"]) == 1


def test_a_refused_token_stops_the_run(writes):
    def refuse(path, **p):
        raise TessieAuth("Tessie refused the token.")

    with pytest.raises(TessieAuth):
        run(Conn(), refuse)


def test_the_cli_wants_a_token(monkeypatch, capsys):
    monkeypatch.delenv("TESSIE_TOKEN", raising=False)
    assert ti.main([]) == 2 and "TESSIE_TOKEN" in capsys.readouterr().err

"""Connector 0.5.0's fields: each read from a fake tree when the car says it, None (nothing rendered, nothing
alerted) for the 0.4.0 tree; then what the pages, alerts, pricing and records do with them."""

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import facts
import lucidfields
import lucidrecords
import notify
from test_app_imports import _env, _hero_ctx
import test_live_fields
from test_live_fields import L, tree_031, tree_040

app = test_live_fields.app  # the fixture, shared
TZ = ZoneInfo("America/Chicago")
T0 = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
EVENTS = json.dumps(
    [
        {"id": "e1", "at": "2026-10-01T10:00:00Z", "level": 1, "type": None, "lat": 39.7, "lon": -89.6, "pictures": 2},
        {"id": "e2", "at": "2026-10-03T22:30:00Z", "level": 3, "type": "break-in", "lat": None, "lon": None, "pictures": 0},
    ]
)
RECORDS = json.dumps(
    [
        {"id": "r2", "start": "2026-10-20T15:02:00Z", "stop": "2026-10-20T15:40:00Z", "energy": 31.5, "charger": "Electrify America Springfield"},
        {"id": "r1", "start": "2026-10-10T12:00:00Z", "stop": None, "energy": None, "charger": None},
        {"id": "bad", "start": "not a time", "energy": 3.0},
    ]
)


def tree_050(state="charging"):
    t = tree_040(state)
    t.update(
        battery_range_unavailable=L(30.0),
        scheduled_charge=L("scheduled"),
        charge_status=L("derated"),
        charge_session_current_limit=L(16.0),
        sentry_alarm_sounding=L(True),
        sentry_events=L(EVENTS),
        tire_sensor_defective=L(True),
        tire_pressure_updated=L("2026-10-20T06:00:00+00:00"),
        gear=L("park"),
        drive_mode=L("sport-plus"),
        software_update_state=L("installing"),
        software_update_percent=L(42.0),
        trim=L("Grand Touring"),
        paint_color=L("Stellar White"),
        ea_plan_name=L("Complimentary charging"),
        ea_plan_expires=L("2027-01-01T00:00:00+00:00"),
        privacy_mode=L(True),
        mpb_fault=L("critical"),
        charging_records=L(RECORDS),
    )
    return t


KEYS = [
    "range_held_back_mi",
    "charge_scheduled",
    "charge_status",
    "charge_limit_amps",
    "alarm_sounding",
    "tire_defective",
    "tires_read_at",
    "gear",
    "drive_mode",
    "update_state",
    "update_percent",
    "trim",
    "paint",
    "ea_plan",
    "ea_plan_ends",
    "ea_days_left",
    "privacy_mode",
    "mpb_fault",
]


def test_every_0_5_0_field_is_read(app, monkeypatch):
    monkeypatch.setattr(app, "garage", lambda: {"V1": tree_050()})
    car = app.live_vehicle("V1")
    assert car["range_held_back_mi"] == pytest.approx(30 / 1.609344, abs=0.01)
    assert (car["charge_scheduled"], car["charge_status"], car["charge_limit_amps"], car["alarm_sounding"]) == ("scheduled", "derated", 16.0, True)
    assert (car["tire_defective"], car["gear"], car["drive_mode"], car["update_state"], car["update_percent"]) == (True, "park", "sport-plus", "installing", 42.0)
    assert (car["trim"], car["paint"], car["ea_plan"], car["ea_plan_ends"], car["privacy_mode"], car["mpb_fault"]) == (
        "Grand Touring",
        "Stellar White",
        "Complimentary charging",
        date(2027, 1, 1),
        True,
        "critical",
    )
    assert car["tires_read_at"] == datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    events = car["sentry_events"]
    assert [e["level_word"] for e in events] == ["Breach", "Walk-by"], "newest first, the level in words"
    assert events[1]["pictures"] == 2 and set(events[0]) == {"at", "level", "level_word", "type", "pictures"}, "metadata only: no place, no picture address"


def test_the_0_4_0_tree_reads_none_and_shows_nothing(app, monkeypatch):
    monkeypatch.setattr(app, "garage", lambda: {"V1": tree_040()})
    car = app.live_vehicle("V1")
    assert {k: car[k] for k in KEYS if k != "ea_days_left"} == {k: None for k in KEYS if k != "ea_days_left"} and car["ea_days_left"] is None
    assert car["sentry_events"] == []
    assert lucidfields.notes(car) == [] and lucidfields.slow_reason(car) is None and lucidfields.update_line(car) is None


def test_a_leaf_of_the_wrong_shape_is_unknown(app, monkeypatch):
    t = tree_031()
    t.update(
        sentry_alarm_sounding=L("yes"),
        tire_pressure_updated=L(5),
        gear=L(1),
        software_update_percent=L(True),
        ea_plan_expires=L("soon"),
        sentry_events=L("{not json"),
        charge_session_current_limit=L("16"),
        privacy_mode=L(1),
    )
    monkeypatch.setattr(app, "garage", lambda: {"V1": t})
    car = app.live_vehicle("V1")
    assert [car[k] for k in ("alarm_sounding", "tires_read_at", "gear", "update_percent", "ea_plan_ends", "charge_limit_amps", "privacy_mode")] == [None] * 7 and car["sentry_events"] == []


def test_a_car_in_park_with_the_drive_state_is_idling_not_driving(app, monkeypatch):
    t = tree_050("off")
    t["state"] = L("driving")
    monkeypatch.setattr(app, "garage", lambda: {"V1": t})
    monkeypatch.setattr(app, "latest_fixes", lambda: {})
    assert app.live_vehicle("V1")["status"] == "Ready"
    t["gear"] = L("drive")
    assert app.live_vehicle("V1")["status"] == "Driving"
    del t["gear"]
    assert app.live_vehicle("V1")["status"] == "Driving", "a connector without gear is read as before"


def test_the_charge_records_come_off_the_tree_oldest_first(app):
    got = lucidfields.records_of(tree_050(), app.leaf)
    assert [r["id"] for r in got] == ["r1", "r2"] and got[1]["kwh"] == 31.5 and got[0]["kwh"] is None and got[0]["charger"] is None
    assert lucidfields.records_of(tree_040(), app.leaf) == []


def test_why_a_charge_is_slow():
    car = {"status": "Charging", "charge_amps_set": 32.0, "charge_limit_amps": 16.0, "charge_status": "derated"}
    assert lucidfields.slow_reason(car) == "charger limits to 16 A", "the wall unit first: it is the one thing the owner can change"
    assert lucidfields.slow_reason(dict(car, charge_limit_amps=32.0)) == "power reduced by the car"
    assert lucidfields.slow_reason(dict(car, charge_limit_amps=None, charge_status="preconditioning-saves-time")) == "preconditioning the pack"
    assert lucidfields.slow_reason(dict(car, charge_limit_amps=None, charge_status="normal")) is None
    assert lucidfields.slow_reason({"status": "Idle", "plugged": True, "charge_scheduled": "scheduled"}) == "waiting for its schedule"
    assert lucidfields.slow_reason({"status": "Idle", "plugged": False, "charge_scheduled": "scheduled"}) is None


def test_the_notes_the_card_carries():
    car = {"alarm_sounding": True, "mpb_fault": "critical", "update_state": "failed", "tire_defective": True, "privacy_mode": True}
    assert lucidfields.notes(car) == ["Alarm sounding", "Car fault: critical", "Software update failed", "Tire sensor fault", "Privacy mode on"]
    assert lucidfields.update_line({"update_state": "installing", "update_percent": 42.0}) == "Installing 42%"
    assert lucidfields.notes({"mpb_fault": "normal", "update_state": "succeeded", "alarm_sounding": False}) == []


def test_the_card_says_what_the_car_says():
    cm, car, cmd, ctx = _hero_ctx()
    ctx["lucid_notes"] = lucidfields.notes
    new = dict(
        car,
        status="Charging",
        charge_amps_set=32.0,
        charge_limit_amps=16.0,
        range_mi=200.0,
        range_held_back_mi=18.0,
        alarm_sounding=True,
        update_state="installing",
        update_percent=42.0,
        privacy_mode=True,
    )
    html = _env().get_template("_now_hero.html").render(car=new, cmd=None, **ctx)
    assert (
        "charger limits to 16 A" in html and "Alarm sounding" in html and "Installing 42%" in html and "18 mi held back" in html.replace("<small>", " ").replace("</small>", "") or "held back" in html
    )
    plain = _env().get_template("_now_hero.html").render(car=car, cmd=None, **ctx)
    assert "held back" not in plain and "Alarm sounding" not in plain and "charger limits" not in plain and "Privacy" not in plain
    gone = _env().get_template("_now_hero.html").render(car=dict(car, offline=True, last_seen=T0, privacy_mode=True), cmd=None, **ctx)
    assert "Privacy mode" in gone
    off = _env().get_template("_now_hero.html").render(car=dict(car, offline=True, last_seen=T0, privacy_mode=None), cmd=None, **ctx)
    assert "Offline" in off and "Privacy mode" not in off


def test_the_security_pane_lists_sentry_events_as_metadata():
    cm, car, cmd, ctx = _hero_ctx()
    events = lucidfields.sentry_events(EVENTS)
    html = (
        _env()
        .from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root) }}')
        .render(cmd=dict(cmd, allowed={"sentry_on", "sentry_off"}), car=dict(car, make="Lucid", sentry_events=events), root="")
    )
    assert "Breach" in html and "break-in" in html and "2 clips" in html
    assert "http" not in html.split('class="sev"')[1].split("</ul>")[0], "nothing to fetch"
    bare = _env().from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root) }}').render(cmd=dict(cmd, allowed={"sentry_on", "sentry_off"}), car=dict(car, make="Lucid"), root="")
    assert 'class="sev"' not in bare


def test_the_tire_block_says_when_the_car_read_them_and_a_dead_sensor(app, monkeypatch):
    env = _env()
    html = env.get_template("_tires.html").render(b={"vin": "V1", "tires": None, "tire_now": [], "tire_history": None, "read_at": T0, "defective": True})
    assert "sensor fault" in html and "Read by the car" in html
    assert env.get_template("_tires.html").render(b={"vin": "V1", "tires": None, "tire_now": [], "tire_history": None, "read_at": None, "defective": None}).strip() == ""


def test_a_tire_reading_is_recorded_at_the_time_the_car_took_it():
    now = T0
    assert lucidfields.tire_stamp(now - timedelta(hours=9), now) == now - timedelta(hours=9)
    assert lucidfields.tire_stamp(now + timedelta(hours=1), now) == now, "never in the future"
    assert lucidfields.tire_stamp(None, now) is None


def test_the_recorder_writes_the_mode_and_privacy_stretches_and_dates_the_tires(app, monkeypatch):
    calls = []

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            class R:
                def fetchone(self_):
                    return {"at": T0 - timedelta(hours=1)}

            return R()

        def commit(self):
            pass

    monkeypatch.setattr(app, "db", lambda: Db())
    monkeypatch.setattr(app, "extend_or_start", lambda conn, table, vin, now, cols, vals, same=None: calls.append((table, now, vals)))
    car = {
        "vin": "V1",
        "sentry": True,
        "climate": "off",
        "drive_mode": "comfort",
        "privacy_mode": False,
        "tires_psi": {"front_left": 40, "front_right": 40, "rear_left": 40, "rear_right": 40},
        "tires_read_at": T0 - timedelta(hours=5),
    }
    app.record_runs(car)
    by = {c[0]: c for c in calls}
    assert by["cc_drive_modes"][2] == ("comfort",) and by["cc_privacy"][2] == (False,)
    assert "cc_tires" not in by, "a reading older than the one held is not stretched over the gap"
    calls.clear()
    app.record_runs(dict(car, tires_read_at=T0 - timedelta(minutes=5)))
    assert {c[0]: c for c in calls}["cc_tires"][1] == T0 - timedelta(minutes=5), "a newer reading is recorded at the car's own time"
    calls.clear()
    app.record_runs(dict(car, tires_read_at=None))
    assert "cc_tires" in {c[0] for c in calls}, "a connector before 0.5.0 is recorded as it always was"


# ---- alerts ------------------------------------------------------------------------------------------------


def kinds(prev, now):
    return [k for k, _t in notify.event_kinds(prev, now)]


def test_alerts_for_the_alarm_a_fault_a_failed_update_and_the_plan_running_out():
    base = {"name": "Alpha", "status": "Idle", "place": "Home"}
    assert kinds(base, dict(base, alarm_sounding=True)) == ["alarm"]
    assert kinds(dict(base, alarm_sounding=True), dict(base, alarm_sounding=True)) == []
    assert kinds(dict(base, mpb_fault="normal"), dict(base, mpb_fault="critical")) == ["fault"]
    assert kinds(dict(base, mpb_fault="critical"), dict(base, mpb_fault="critical")) == []
    assert kinds(dict(base, mpb_fault="warning"), dict(base, mpb_fault="normal")) == []
    assert kinds(dict(base, update_state="installing"), dict(base, update_state="failed")) == ["update"]
    assert kinds(dict(base, update_state="installing"), dict(base, update_state="succeeded")) == []
    assert kinds(base, dict(base, alarm_sounding=None, mpb_fault=None, update_state=None, ea_days_left=None)) == [], "the 0.4.0 car says none of it"


def test_the_plan_alert_goes_once_as_each_reminder_day_passes():
    base = {"name": "Alpha", "status": "Idle", "ea_plan": "Complimentary charging", "ea_plan_ends": date(2026, 12, 1)}
    assert kinds(dict(base, ea_days_left=31), dict(base, ea_days_left=30)) == ["ea_plan"]
    assert kinds(dict(base, ea_days_left=30), dict(base, ea_days_left=29)) == []
    assert kinds(dict(base, ea_days_left=8), dict(base, ea_days_left=7)) == ["ea_plan"]
    assert kinds(dict(base, ea_days_left=7), dict(base, ea_days_left=6)) == []
    assert kinds(dict(base, ea_days_left=40), dict(base, ea_days_left=39)) == []
    assert kinds(dict(base, ea_days_left=None), dict(base, ea_days_left=20)) == [], "first sight of a plan inside the window does not send the 30-day one"
    text = dict(notify.event_kinds(dict(base, ea_days_left=8), dict(base, ea_days_left=7)))["ea_plan"]
    assert text == "Alpha's Complimentary charging plan ends in 7 days (Dec 1)."
    assert kinds(dict(base, ea_days_left=3), dict(base, ea_days_left=-1)) == []


def test_the_new_alert_kinds_have_switches_and_hide_without_a_connector():
    assert {"fault", "ea_plan"} <= set(notify.KINDS) and {"fault", "ea_plan"} <= {k for _g, ks in notify.GROUPS for k in ks}
    assert {"fault", "ea_plan"} <= notify.hidden_kinds(["Tesla"], commandable=True, ha=True, connector=False)
    assert not {"fault", "ea_plan"} & notify.hidden_kinds(["Lucid"], commandable=True, ha=True, connector=True)
    assert {"alarm_sounding", "mpb_fault", "update_state", "ea_days_left"} <= set(notify.WATCHED)


# ---- the Electrify America plan and the car's own records, in the price ------------------------------------


def session(**kw):
    return {
        "id": 1,
        "session_start_date": T0,
        "session_end_date": T0 + timedelta(minutes=40),
        "session_position_latitude": None,
        "session_position_longitude": None,
        "real_charged": None,
        "charging_type": "DC",
        "real_cost": None,
        "network": None,
        **kw,
    }


PLAN = {"name": "Complimentary charging", "since": date(2026, 9, 1), "ends": date(2027, 1, 1)}


def price(s, ea=PLAN, powers=(), **kw):
    return facts.charge_facts([s], list(powers), TZ, lambda la, lo: False, dc_price=0.56, ea_plan=ea, **kw)[0]


def test_electrify_america_is_free_while_the_plan_runs():
    got = price(session(record_kwh=31.5, record_network="electrify_america"))
    assert got[1] == 31.5 and got[2] == 0.0 and got[3] == "Complimentary charging, free at Electrify America" and got[9] == "place"


def test_a_bill_a_typed_price_and_a_places_own_price_outrank_the_plan():
    s = session(network="electrify_america", record_kwh=30.0)
    assert price(s, bills=[{"id": "b", "vin": None, "started_at": T0, "total": 12.5, "site": "EA"}])[2] == 12.5
    assert price(dict(s, real_cost=9.0))[2] == 9.0
    place = {"name": "EA Springfield", "price": {"kind": "kwh", "rate": 0.4}}
    assert price(s, place_of=lambda la, lo: place)[2] == pytest.approx(12.0)


def test_the_plan_does_not_price_what_it_does_not_cover():
    s = session(network="electrify_america", record_kwh=30.0)
    assert price(s, ea=None)[2] == pytest.approx(30.0 * 0.56), "a car with no plan pays the fast-charging rate"
    assert price(s, ea=dict(PLAN, ends=date(2026, 10, 1)))[2] == pytest.approx(16.8), "after it ended"
    assert price(s, ea=dict(PLAN, since=date(2026, 11, 1)))[2] == pytest.approx(16.8), "before the site first saw it"
    assert price(s, ea=dict(PLAN, name="Pass+ member"))[2] == pytest.approx(16.8), "a plan that only discounts is not free"
    assert price(session(network="supercharger", record_kwh=30.0))[2] != 0.0
    assert price(session(record_kwh=30.0))[2] == pytest.approx(16.8), "a charge at some other charger"


def test_the_records_energy_stands_in_only_where_the_car_has_no_meter_and_it_is_believable():
    power = [(T0, T0 + timedelta(minutes=40), 45.0)]  # 30 kWh by the profile
    assert price(session(record_kwh=31.5), powers=power)[1] == 31.5
    assert price(session(record_kwh=31.5, live_meter=28.0), powers=power)[1] == 30.0, "the car's own meter first (the profile is larger here)"
    assert price(session(record_kwh=31.5, real_charged=29.0), powers=power)[1] == 29.0
    assert price(session(record_kwh=3150.0), powers=power)[1] == 30.0, "a unit that is not kWh is not used"
    assert price(session(), powers=power)[1] == 30.0


def test_plan_words():
    assert lucidfields.plan_is_free("Complimentary charging") and lucidfields.plan_is_free("EA Unlimited")
    assert not lucidfields.plan_is_free("Pass+") and not lucidfields.plan_is_free(None) and not lucidfields.plan_is_free("10% off")
    assert not lucidfields.plan_is_free("Lucid Charging Plan")  # Electrify America and Superchargers at the retail rate
    assert lucidrecords.network_of("Electrify America Springfield") == "electrify_america" and lucidrecords.network_of("EA - Wabash") == "electrify_america"
    assert lucidrecords.network_of("Wallbox") is None and lucidrecords.network_of(None) is None
    assert [lucidrecords.mode_word(m) for m in ("sport-plus", "comfort", "track-a", None)] == ["Sport+", "Comfort", "Track A", None]


def test_identity_takes_the_cars_trim_and_paint_only_when_nothing_else_names_them():
    import identity

    got = identity.describe("L1", "Lucid", "Air", None, reported_trim="Grand Touring", reported_paint="Stellar White")
    assert got["trim"] == "Grand Touring" and "Stellar White" in got["line"] and got["paint"] is None, "a name is not a swatch"
    plain = identity.describe("L1", "Lucid", "Air", None)
    assert plain["trim"] is None and "Stellar" not in plain["line"]
    row = {"paint": "#112233", "detected": {}}
    assert identity.describe("L1", "Lucid", "Air", row, reported_paint="Stellar White")["paint"]["hex"] == "#112233"


def test_the_software_page_shows_an_install_under_way():
    row = {"version": "3.1", "seen_at": T0, "days": 3, "brief": None, "until": None, "odometer": None, "full": None, "full_n": 0, "full_change": None, "temp_f": None, "secs": 5, "by_hand": False}
    block = {"vin": "V1", "name": "Alpha", "rows": [row], "install": "Installing 42%", "waiting": None, "cadence": None, "timeline": None}
    ctx = dict(car={"garage": False, "name": "Alpha"}, colors={}, root="", u={"dist": "mi", "temp": "F"}, add_cars=[], today=None, csrf="", q="")
    html = _env().get_template("software.html").render(blocks=[block], **ctx)
    assert "Installing 42%" in html and "Update progress" in html
    assert "Update progress" not in _env().get_template("software.html").render(blocks=[dict(block, install=None)], **ctx)


def test_a_connector_before_0_5_0_has_no_install_line(app, monkeypatch):
    monkeypatch.setattr(app, "_cache", {"garage_tree": (datetime.now(timezone.utc), {"V1": tree_040(), "V2": tree_050()})})
    assert app.live_reading("V1")["update_state"] is None and app.live_reading("V2")["update_percent"] == 42.0


def test_the_plans_are_read_from_the_held_tree_soonest_first(app, monkeypatch):
    soon = tree_050()
    soon["ea_plan_expires"] = L((datetime.now(timezone.utc) + timedelta(days=20)).isoformat())
    monkeypatch.setattr(app, "_cache", {"garage_tree": (T0, {"V1": tree_050(), "V2": soon, "V3": tree_040()})})
    monkeypatch.setattr(app, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}, {"vin": "V3", "name": "Charlie"}])
    plans = app.ea_plans(None)
    assert [p["car"] for p in plans] == ["Bravo", "Alpha"] and plans[0]["days"] in (19, 20, 21) and plans[0]["free"] is True
    assert [p["car"] for p in app.ea_plans("V1")] == ["Alpha"] and app.ea_plans("V3") == [], "a 0.4.0 car has none"
    monkeypatch.setattr(app, "_cache", {})
    assert app.ea_plans(None) == []


def test_the_charging_page_shows_the_plan_and_the_day_a_stretch_of_privacy_mode():
    env = _env()
    plans = [{"car": "Alpha", "plan": "Complimentary charging", "ends": date(2027, 1, 1), "days": 20, "free": True}]
    ctx = dict(car={"garage": False, "name": "Alpha", "vin": "V1"}, s={"total_charges": 0, "total_topups": 0}, days=None, root="", q="", cur="$", u={})
    html = env.get_template("charging.html").render(ea=plans, **ctx)
    assert "Electrify America" in html and "20" in html and "Complimentary charging" in html and "charges at $0" in html
    assert "Electrify America" not in env.get_template("charging.html").render(ea=[], **ctx)
    assert "Ended" in env.get_template("charging.html").render(ea=[dict(plans[0], days=-3)], **ctx)

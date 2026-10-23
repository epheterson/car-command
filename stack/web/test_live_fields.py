"""What live_vehicle reads from the connector's tree, connector 0.4.0's fields above all: each
one there when the car says it, None when it does not, and None for 0.3.1 and any other car. Then
the recorder's writes for the two that only last while the car says them."""

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

NEW = {
    "charge_session_kwh": None,
    "charge_full_at": None,
    "alarm_status": None,
    "sentry_paused": None,
    "sentry_usb": None,
    "living_object": None,
    "occupant_warning": None,
    "keyfob_low": None,
    "window_heating": None,
    "battery_health": None,
    "update_available": None,
    "update_version": None,
    "update_install_min": None,
    "update_notes": None,
    "update_notes_url": None,
    "elevation_raw": None,
}


def L(v):
    return {"val": v}


def tree_031(state="charging"):
    return {
        "name": L("Alpha"),
        "manufacturer": L("Lucid"),
        "state": L("parked"),
        "connection_state": L("reachable"),
        "battery_capacity_kwh": L(112.0),
        "drives": {"primary": {"level": L(55.0)}},
        "charging": {"state": L(state), "power": L(11.2), "settings": {"target_level": L(80.0)}, "connector": {"connection_state": L("connected")}},
        "sentry": L(True),
    }


def tree_040(state="charging"):
    t = tree_031(state)
    t["charging"]["estimated_date_reached"] = L((datetime.now(timezone.utc) + timedelta(minutes=95)).isoformat())
    t["charging"]["connector"]["lock_state"] = L("locked")
    t["window_heating"] = {"heating_state": L("on")}
    t.update(
        charge_session_energy=L(18.42),
        alarm_status=L("armed"),
        sentry_inactive_reason=L("usb-full"),
        sentry_usb_drive_connected=L(True),
        living_object_detection=L("disabled"),
        unattended_occupant_detection=L("no-request"),
        keyfob_battery_low=L(False),
        battery_health_level=L(100.0),
        software_update_available=L(True),
        software_update_version=L("3.1.2"),
        software_update_install_time=L(1800.0),
        software_update_release_notes=L("Faster."),
        software_update_release_notes_url=L("https://example.com/n"),
        gps_elevation_raw=L(8932.0),
    )
    return t


@pytest.fixture
def app(monkeypatch):
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    a = importlib.import_module("app")
    monkeypatch.setattr(a, "places", lambda: [])
    return a


def test_connector_0_4_0s_fields_are_read(app, monkeypatch):
    monkeypatch.setattr(app, "garage", lambda: {"V1": tree_040()})
    car = app.live_vehicle("V1")
    got = {k: car[k] for k in NEW}
    full_at = got.pop("charge_full_at")
    assert got == {
        "charge_session_kwh": 18.42,
        "alarm_status": "armed",
        "sentry_paused": "usb-full",
        "sentry_usb": True,
        "living_object": "disabled",
        "occupant_warning": "no-request",
        "keyfob_low": False,
        "window_heating": True,
        "battery_health": 100.0,
        "update_available": True,
        "update_version": "3.1.2",
        "update_install_min": 30,
        "update_notes": "Faster.",
        "update_notes_url": "https://example.com/n",
        "elevation_raw": 8932.0,
    }
    assert car["charge_minutes_left"] in (94, 95) and abs((full_at - datetime.now(timezone.utc)).total_seconds() - car["charge_minutes_left"] * 60) < 5


def test_connector_0_3_1_reads_none_of_them(app, monkeypatch):
    monkeypatch.setattr(app, "garage", lambda: {"V1": tree_031("off")})
    car = app.live_vehicle("V1")
    assert {k: car[k] for k in NEW} == NEW


def test_a_value_of_the_wrong_shape_is_unknown(app, monkeypatch):
    """Another brand's connector, or a later change, publishing a leaf of the same name differently."""
    t = tree_031()
    t.update(charge_session_energy=L("18"), keyfob_battery_low=L("low"), battery_health_level=L(True), software_update_install_time=L("soon"), alarm_status=L(3), gps_elevation_raw=L("8932"))
    monkeypatch.setattr(app, "garage", lambda: {"V1": t})
    car = app.live_vehicle("V1")
    assert [car[k] for k in ("charge_session_kwh", "keyfob_low", "battery_health", "update_install_min", "alarm_status", "elevation_raw")] == [None] * 6


def test_the_cached_tree_answers_the_software_page_without_a_call(app, monkeypatch):
    monkeypatch.setattr(app, "_cache", {"garage_tree": (datetime.now(timezone.utc), {"V1": tree_040()})})
    monkeypatch.setattr(app, "garage", lambda: pytest.fail("the connector was asked again"))
    assert app.live_reading("V1")["update_version"] == "3.1.2" and app.live_reading("V9") is None
    monkeypatch.setattr(app, "_cache", {})
    assert app.live_reading("V1") is None


class Db:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql.split()[2])
        return self

    def commit(self):
        pass


def test_the_recorder_keeps_the_meter_while_charging_and_the_health_once_a_day(app, monkeypatch):
    opened, met, db = [], [], Db()
    monkeypatch.setattr(app, "db", lambda: opened.append(1) or db)
    monkeypatch.setattr(app, "take_meter", lambda conn, vin, kwh, now: met.append((vin, kwh)))
    monkeypatch.setattr(app, "HEALTH", app.healthlog.HealthLog())
    car = {"vin": "V1", "live": True, "status": "Charging", "charge_session_kwh": 18.42, "battery_health": 100.0}
    app.record_car_figures(car)
    app.record_car_figures(dict(car, charge_session_kwh=18.9))
    assert met == [("V1", 18.42), ("V1", 18.9)], "every reading while charging"
    assert db.sql == ["cc_battery_health"] and app.HEALTH.series["V1"][0][1] == 100.0, "the health once"
    opened.clear()
    app.record_car_figures(dict(car, status="Idle", charge_session_kwh=None))
    assert opened == [], "parked, the day's health logged: no connection at all"
    app.record_car_figures({"vin": "T1", "live": False, "status": "Charging", "charge_session_kwh": None})
    app.record_car_figures({"vin": "T1", "live": True, "status": "Charging"})
    assert opened == [], "a Tesla, or connector 0.3.1, writes nothing"


def test_a_health_figure_whose_commit_fails_is_written_again_next_tick(app, monkeypatch):
    class Failing(Db):
        def commit(self):
            raise RuntimeError("the database went away")

    monkeypatch.setattr(app, "HEALTH", app.healthlog.HealthLog())
    monkeypatch.setattr(app, "db", lambda: Failing())
    car = {"vin": "V1", "live": True, "status": "Idle", "battery_health": 100.0}
    with pytest.raises(RuntimeError):
        app.record_car_figures(car)
    assert app.HEALTH.series == {}
    good = Db()
    monkeypatch.setattr(app, "db", lambda: good)
    app.record_car_figures(car)
    assert good.sql == ["cc_battery_health"] and len(app.HEALTH.series["V1"]) == 1

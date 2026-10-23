"""Whether a car on the direct stream is locked, its doors and its windows: asked for, decoded, carried and shown.

Field and value names are Tesla's, from teslamotors/fleet-telemetry protos/vehicle_data.proto: Field DoorState (58),
Locked (59), FdWindow, FpWindow, RdWindow, RpWindow (60-63); Value door_value (a Doors message of six bools),
window_state_value (WindowStateClosed, WindowStatePartiallyOpen, WindowStateOpened) and boolean_value."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import activity
import tesla_live
import tesla_net
import tesla_stream as ts
from test_tesla_live import ALPHA, frames, stream

WINDOWS = ("FdWindow", "FpWindow", "RdWindow", "RpWindow")


def mins(m):
    return datetime(2026, 10, 6, 17, 30, tzinfo=timezone.utc) - timedelta(minutes=m)


# ---- what is asked of the car


def test_the_car_is_asked_for_its_lock_doors_and_windows():
    f = tesla_net.FIELDS
    assert f["Locked"] == 10 and f["DoorState"] == 10, "sent when they change, at most this often"
    assert all(f[k] == 60 for k in WINDOWS)
    cfg = tesla_net.config("stream.example.test", now=1_000_000)
    assert cfg["fields"]["Locked"] == {"interval_seconds": 10} and cfg["fields"]["FdWindow"] == {"interval_seconds": 60}


def test_a_car_sent_the_old_list_is_told_its_config_is_out_of_date():
    from teslapage import outdated

    old = {k: v for k, v in tesla_net.FIELDS.items() if k not in ("Locked", "DoorState", *WINDOWS)}
    assert outdated({"fields": {k: {"interval_seconds": v} for k, v in old.items()}}, None) is True
    assert tesla_net.fields_version(old) != tesla_net.fields_version()
    assert outdated(None, tesla_net.fields_version(old)) is True and outdated(None, tesla_net.fields_version()) is False


# ---- how the values arrive


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"booleanValue": True}, True),
        ({"booleanValue": False}, False),
        ({"boolean_value": True}, True),
        ({"stringValue": "true"}, "true"),
        ({"stringValue": "false"}, "false"),
        ({"doorValue": {"DriverFront": True}}, {"DriverFront": True}),
        ({"doorValue": {"driverFront": True, "trunk_rear": True, "PassengerRear": False}}, {"DriverFront": True, "TrunkRear": True, "PassengerRear": False}),
        ({"door_value": {"passenger_front": True}}, {"PassengerFront": True}),
        ({"doorValue": {}}, {}),  # protojson leaves out every false: all closed
        ({"windowStateValue": "WindowStateClosed"}, "Closed"),
        ({"windowStateValue": "WindowStatePartiallyOpen"}, "PartiallyOpen"),
        ({"window_state_value": "WindowStateOpened"}, "Opened"),
        ({"windowStateValue": "WindowStateUnknown"}, "Unknown"),
        ({"invalid": True}, ts.INVALID),
    ],
)
def test_lock_door_and_window_values_decode(value, expected):
    assert ts.value_of(value) == expected


def record(**fields):
    data = []
    for key, value in fields.items():
        data.append({"key": key, "value": value})
    return {"data": data, "createdAt": "2026-09-07T15:00:00Z", "vin": ALPHA, "isResend": False}


def test_the_stream_carries_them_and_an_invalid_value_clears_them():
    s = stream()
    s.handle(frames(record(Locked={"booleanValue": True}, DoorState={"doorValue": {"TrunkRear": True}}, FdWindow={"windowStateValue": "WindowStateClosed"})))
    c = s.carried[ALPHA]
    assert c["Locked"] is True and c["DoorState"] == {"TrunkRear": True} and c["FdWindow"] == "Closed"
    s.handle(frames({**record(Locked={"booleanValue": False}), "createdAt": "2026-09-07T15:05:00Z"}))
    assert s.carried[ALPHA]["Locked"] is False, "unlocked is a value, not an absence"
    s.handle(frames({**record(Locked={"invalid": True}), "createdAt": "2026-09-07T15:06:00Z"}))
    assert "Locked" not in s.carried[ALPHA], "the car cannot say now: unknown, not the last answer"
    assert {"Locked", "DoorState", *WINDOWS} <= set(tesla_live.LIVE_FIELDS)


# ---- what Now shows


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("false", False),
        ("False", False),
        (1.0, True),
        (0.0, False),
        ("locked", True),
        ("unlocked", False),
        ("", None),
        ("maybe", None),
        (None, None),
    ],
)
def test_locked_is_true_false_or_unknown(raw, expected):
    assert activity.truth(raw) is expected
    assert activity.stream_now({"_at": mins(1), "Locked": raw}, mins(0))["locked"] is expected


def test_a_stream_that_never_said_leaves_the_state_unsaid():
    st = activity.stream_now({"_at": mins(1), "Gear": "P"}, mins(0))
    assert "locked" not in st and "doors_open" not in st and "windows_open" not in st


def test_doors_and_windows_open():
    now = mins(0)
    assert activity.stream_now({"_at": mins(1), "DoorState": {}}, now)["doors_open"] is False
    assert activity.stream_now({"_at": mins(1), "DoorState": {"DriverFront": False}}, now)["doors_open"] is False
    assert activity.stream_now({"_at": mins(1), "DoorState": {"DriverFront": False, "TrunkFront": True}}, now)["doors_open"] is True
    opened = activity.stream_now({"_at": mins(1), "DoorState": {"TrunkFront": True}}, now)
    assert opened["frunk_open"] is True and opened["trunk_open"] is False, "the buttons say which way each goes"
    closed = {"_at": mins(1), **{k: "Closed" for k in WINDOWS}}
    assert activity.stream_now(closed, now)["windows_open"] is False
    assert activity.stream_now({**closed, "RpWindow": "PartiallyOpen"}, now)["windows_open"] is True
    assert activity.stream_now({**closed, "FdWindow": "Opened"}, now)["windows_open"] is True
    assert activity.stream_now({"_at": mins(1), **{k: "Unknown" for k in WINDOWS}}, now)["windows_open"] is None
    assert activity.stream_now({"_at": mins(1), "FdWindow": "Closed"}, now)["windows_open"] is False


def test_a_sleeping_car_keeps_its_last_word_on_the_lock_with_when_it_said_it():
    # a car asleep changes none of these; the time lets a reader judge how old the word is
    st = activity.stream_now({"_at": mins(300), "Locked": True, "FdWindow": "Closed", "ChargeLimitSoc": 80, "Gear": "D"}, mins(0))
    assert st["locked"] is True and st["windows_open"] is False and st["charge_limit"] == 80.0
    assert st["said_at"] == mins(300) and st["fresh"] is False and "driving" not in st


class Rows:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [] if self.row is None else [self.row]


class VehicleDb:
    """Just a vehicle row, and nothing else on record for it."""

    def execute(self, sql="", *a, **k):
        text = " ".join(str(sql).lower().split())
        if "from vehicles where vin" in text:
            return Rows({"vin": ALPHA, "name": "Alpha", "manufacturer": "Tesla", "model": "Model S"})
        if "to_regclass" in text:
            return Rows({"t": None})
        return Rows(None)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def car(monkeypatch):
    from test_empty import bare  # noqa: F401 - the app module, imported the way the other tests do

    import importlib
    import os
    import sys
    from pathlib import Path

    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(Path(__file__).parent))
    app = importlib.import_module("app")
    monkeypatch.setattr(app, "db", lambda: VehicleDb())
    monkeypatch.setattr(app, "places", lambda: [])
    now = datetime.now(timezone.utc)
    monkeypatch.setitem(app.tesla_stream_worker.carried, ALPHA, {"_at": now - timedelta(minutes=1)})
    return app, app.tesla_stream_worker.carried[ALPHA]


def test_the_cars_state_has_locked_true_false_or_none(car):
    app, carried = car
    assert app.stored_vehicle(ALPHA)["locked"] is None, "the stream has not said"
    carried["Locked"] = True
    now = app.stored_vehicle(ALPHA)
    assert now["locked"] is True and now["doors_open"] is False and now["windows_open"] is None
    carried.update(Locked=False, DoorState={"DriverFront": True}, FdWindow="PartiallyOpen")
    now = app.stored_vehicle(ALPHA)
    assert now["locked"] is False and now["doors_open"] is True and now["windows_open"] is True
    assert json.loads(json.dumps(app.jsonable(now)))["locked"] is False, "what /api/now answers"
    carried.pop("Locked")
    assert app.stored_vehicle(ALPHA)["locked"] is None, "unknown again, not the last answer"


def test_the_stored_car_reads_its_newest_runs_on_their_indexes(car, monkeypatch):
    """Ordered by last_date, which no index covers, the level, range and state lookups scanned
    their whole tables on every Tesla page: 50 ms against a quarter of a millisecond."""
    app, _ = car
    seen = []

    class Recording(VehicleDb):
        def execute(self, sql="", *a, **k):
            seen.append(" ".join(str(sql).split()))
            return super().execute(sql, *a, **k)

    monkeypatch.setattr(app, "db", lambda: Recording())
    app.stored_vehicle(ALPHA)
    runs = [s for s in seen if "FROM drive_levels" in s or "FROM drive_ranges" in s or "FROM states" in s]
    assert len(runs) == 3 and all("ORDER BY l.first_date DESC" in s or "ORDER BY first_date DESC" in s for s in runs), runs
    assert not [s for s in runs if "JOIN drives" in s], "the drive id is resolved first, so the index on it is used"


class FeedDb(VehicleDb):
    """A vehicle with a TeslaFi feed whose newest full row is `row`."""

    def __init__(self, row):
        self.row = row

    def execute(self, sql="", *a, **k):
        text = " ".join(str(sql).lower().split())
        if "to_regclass" in text:
            return Rows({"t": "cc_teslafi_rows"})
        if "from cc_teslafi_rows" in text:
            return Rows(self.row)
        return super().execute(sql, *a, **k)


def feed_row(at, **kw):
    base = {
        "at": at,
        "fsd": None,
        "trip": None,
        "inside": "68.5",
        "outside": "55.4",
        "locked": "1",
        "sentry": "0",
        "charging_state": "Disconnected",
        "charge_limit": "80",
        "car_state": "Sleeping",
        "level": "70",
        "kwh_added": None,
        "lat": None,
        "lon": None,
        "fd": "0",
        "fp": "0",
        "rd": "0",
        "rp": "0",
    }
    return {**base, **kw}


def test_where_the_stream_has_not_said_the_feed_gives_the_lock_and_windows_and_fresh_temperatures(car, monkeypatch):
    app, carried = car
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(app, "db", lambda: FeedDb(feed_row(now - timedelta(minutes=5), rp="1")))
    got = app.stored_vehicle(ALPHA)
    assert got["locked"] is True and got["windows_open"] is True and got["inside_f"] == 68.5 and got["outside_f"] == 55.4
    assert got["locked_at"] == now - timedelta(minutes=5)
    carried.update(Locked=False)  # the car's own word wins
    assert app.stored_vehicle(ALPHA)["locked"] is False
    monkeypatch.setattr(app, "db", lambda: FeedDb(feed_row(now - timedelta(hours=3))))
    old = app.stored_vehicle(ALPHA)
    assert old["inside_f"] is None and old["outside_f"] is None  # an old reading is not the cabin now

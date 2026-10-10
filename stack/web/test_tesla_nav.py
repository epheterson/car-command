"""Where the driver told the car it was going: per drive, and the names it gives places."""

from datetime import datetime, timedelta, timezone

import pytest

import tesla_nav as tn
import tesla_net
import tesla_stream as ts
import teslapage
from places import PlaceIndex

ALPHA = "5YJ3E1EA0JF000001"
BRAVO = "5YJ3E1EA0JF000002"
T0 = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
HOME = (39.7800, -89.6500)  # Springfield, made up
MARKET = (39.8000, -89.6300)  # Bravo Market, about 2.8 km away
NAMES = {ALPHA: "Alpha", BRAVO: "Bravo"}


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def payload(minutes, vin=ALPHA, **fields):
    """A record as the receiver's JSON gives it (protojson, prefer_typed)."""
    data = []
    for key, value in fields.items():
        if value is None:
            data.append({"key": key, "value": {"invalid": True}})
        elif isinstance(value, tuple):
            data.append({"key": key, "value": {"locationValue": {"latitude": value[0], "longitude": value[1]}}})
        elif isinstance(value, str) and value.startswith("ShiftState"):
            data.append({"key": key, "value": {"shiftStateValue": value}})
        elif isinstance(value, str):
            data.append({"key": key, "value": {"stringValue": value}})
        else:
            data.append({"key": key, "value": {"doubleValue": value}})
    return {"data": data, "createdAt": at(minutes), "vin": vin, "isResend": False}


def drive(payloads_before=(), during=(), after=(), end=MARKET):
    """Parked at home, a drive from minute 10 to 30 ending at `end`, parked after."""
    ps = [payload(0, Location=HOME, Gear="ShiftStateP", VehicleSpeed=0.0)]
    ps += list(payloads_before)
    ps += [payload(10, Gear="ShiftStateD", VehicleSpeed=20.0)]
    ps += [payload(20, Location=(39.79, -89.64), VehicleSpeed=30.0)]
    ps += list(during)
    ps += [payload(30, Location=end, Gear="ShiftStateP", VehicleSpeed=0.0)]
    ps += list(after)
    recs = ts.records(ps)
    return tn.per_drive(recs, ts.samples(recs, until=T0 + timedelta(minutes=60)))


# ---- the fields
def test_the_stream_asks_for_the_route():
    for k in ("DestinationName", "DestinationLocation", "OriginLocation", "MinutesToArrival", "MilesToArrival"):
        assert k in tesla_net.FIELDS
    assert tesla_net.FIELDS["MinutesToArrival"] == 30 and tesla_net.FIELDS["DestinationName"] <= 10
    assert tesla_net.config("stream.example.test")["fields"]["DestinationName"] == {"interval_seconds": tesla_net.FIELDS["DestinationName"]}


def test_a_change_to_the_fields_changes_their_version():
    v = tesla_net.fields_version()
    assert v == tesla_net.fields_version(dict(tesla_net.FIELDS)) and len(v) == 12
    assert v != tesla_net.fields_version({**tesla_net.FIELDS, "MinutesToArrival": 60})
    assert v != tesla_net.fields_version({k: x for k, x in tesla_net.FIELDS.items() if k != "DestinationName"})


def test_a_config_is_outdated_by_what_tesla_holds_or_else_by_what_was_sent():
    now = tesla_net.field_spec()
    old = {k: v for k, v in now.items() if not k.startswith(("Destination", "Origin", "Route", "Minutes", "Miles"))}
    assert teslapage.outdated({"fields": now}, None) is False, "Tesla's own copy decides when it says"
    assert teslapage.outdated({"fields": old}, tesla_net.fields_version()) is True
    assert teslapage.outdated({"synced": True}, tesla_net.fields_version()) is False
    assert teslapage.outdated({"synced": True}, None) is True, "a car started before the list was kept is older than it"


def test_the_background_check_sends_once_and_only_to_started_cars():
    now = tesla_net.fields_version()
    assert teslapage.to_update([ALPHA, BRAVO], {ALPHA: "old", BRAVO: now}, {}, now) == [ALPHA]
    assert teslapage.to_update([ALPHA], {ALPHA: "old"}, {ALPHA: now}, now) == [], "tried with this list already: not again"
    assert teslapage.to_update([], {ALPHA: "old"}, {}, now) == [], "never a car not started"


def test_stream_cars_marks_only_started_cars_outdated():
    f = {
        "cars": [{"vin": ALPHA, "name": "Alpha", "paired": True}, {"vin": BRAVO, "name": "Bravo", "paired": True}],
        "stream_cars": {ALPHA: {"on": True, "config": {"synced": True}, "sent": "old"}, BRAVO: {"on": False, "config": None, "sent": None}},
    }
    got = {c["vin"]: c["outdated"] for c in teslapage.stream_cars(f)}
    assert got == {ALPHA: True, BRAVO: False}


# ---- reading the values
def test_values_decode_from_the_records():
    recs = ts.records([payload(1, DestinationName="Bravo Market", DestinationLocation=MARKET, MinutesToArrival=12.4), payload(2, DestinationName=None)])
    assert recs[0][2] == {"DestinationName": "Bravo Market", "DestinationLocation": MARKET, "MinutesToArrival": 12.4}
    assert recs[1][2]["DestinationName"] is ts.INVALID
    assert tn.text_of(76.0) == "76", "a station called 76 is a name, not a number"
    assert tn.text_of(ts.INVALID) is None and tn.text_of("  Bravo   Market ") == "Bravo Market"


@pytest.mark.parametrize(
    "raw,name",
    [
        ("Bravo Market, 12 Elm Street, Springfield, IL 62701", "Bravo Market"),
        ("742 Evergreen Terrace, Springfield, IL", "742 Evergreen Terrace, Springfield, IL"),
        ("Springfield, IL", "Springfield, IL"),
        ("Home", "Home"),
        ("Charlie's Diner", "Charlie's Diner"),
        (76.0, "76"),
        ("", None),
        (None, None),
    ],
)
def test_destination_names_map_to_place_names(raw, name):
    assert tn.clean_name(raw) == name


# ---- per drive
def test_the_destination_set_before_the_drive_counts_for_it():
    rows = drive(payloads_before=[payload(5, DestinationName="Bravo Market", DestinationLocation=MARKET, OriginLocation=HOME)])
    assert len(rows) == 1
    r = rows[0]
    assert r["vin"] == ALPHA and r["started"] == T0 + timedelta(minutes=10) and r["ended"] == T0 + timedelta(minutes=30)
    assert r["dest_name"] == "Bravo Market" and (r["dest_lat"], r["dest_lon"]) == MARKET and (r["origin_lat"], r["origin_lon"]) == HOME
    assert (r["end_lat"], r["end_lon"]) == MARKET


def test_a_route_changed_on_the_way_last_one_wins():
    rows = drive(
        payloads_before=[payload(5, DestinationName="Charlie's Diner", DestinationLocation=(39.9, -89.5))],
        during=[payload(22, DestinationName="Bravo Market", DestinationLocation=MARKET)],
    )
    assert rows[0]["dest_name"] == "Bravo Market" and rows[0]["dest_lat"] == MARKET[0]


def test_a_route_cleared_on_arrival_still_says_where_it_went():
    rows = drive(
        payloads_before=[payload(5, DestinationName="Bravo Market", DestinationLocation=MARKET)],
        during=[payload(29, DestinationName=None, DestinationLocation=None, MinutesToArrival=None)],
    )
    assert rows[0]["dest_name"] == "Bravo Market"


def test_a_route_cleared_before_the_drive_is_no_route():
    rows = drive(payloads_before=[payload(3, DestinationName="Bravo Market", DestinationLocation=MARKET), payload(6, DestinationName=None, DestinationLocation=None)])
    assert rows == []


def test_a_drive_with_no_navigation_keeps_no_row():
    assert drive() == []


def test_a_drive_cut_by_the_window_start_is_left_out():
    ps = [payload(0, Location=HOME, Gear="ShiftStateD", VehicleSpeed=20.0, DestinationName="Bravo Market"), payload(5, Location=MARKET, Gear="ShiftStateP", VehicleSpeed=0.0)]
    recs = ts.records(ps)
    assert tn.per_drive(recs, ts.samples(recs, until=T0 + timedelta(minutes=30))) == []


class FakeConn:
    def __init__(self):
        self.sent = []

    def cursor(self):
        return self

    def executemany(self, sql, rows):
        self.sent.append((sql, rows))


def test_save_upserts_by_car_and_drive_start():
    conn = FakeConn()
    rows = drive(payloads_before=[payload(5, DestinationName="Bravo Market", DestinationLocation=MARKET)])
    assert tn.save(conn, rows) == 1 and tn.save(conn, []) == 0
    sql, sent = conn.sent[0]
    assert "ON CONFLICT (vin, started) DO UPDATE" in sql and sent[0][:2] == (ALPHA, T0 + timedelta(minutes=10))


# ---- naming
def row(name, end=MARKET, dest=MARKET, vin=ALPHA, minutes=0):
    return {
        "vin": vin,
        "started": T0 + timedelta(minutes=minutes),
        "ended": T0 + timedelta(minutes=minutes + 20),
        "end_lat": end[0],
        "end_lon": end[1],
        "dest_name": name,
        "dest_lat": dest[0] if dest else None,
        "dest_lon": dest[1] if dest else None,
    }


def east(p, metres):
    return (p[0], p[1] + metres / 85_600.0)  # a degree of longitude near 39.8 N is about 85.6 km


FOUND = {"id": 7, "name": "Elm Street, Springfield", "latitude": MARKET[0], "longitude": MARKET[1], "radius_m": 150.0, "auto": True}


def test_a_drive_that_ended_near_its_destination_names_the_place():
    got = tn.place_suggestions([row("Bravo Market, 12 Elm Street, Springfield", end=east(MARKET, 100), dest=east(MARKET, 200))], [FOUND], NAMES)
    assert got[7]["name"] == "Bravo Market" and got[7]["source"] == "from Alpha's navigation" and got[7]["alternates"] == []


def test_a_drive_that_ended_far_from_its_destination_names_nothing():
    far = east(MARKET, 400)
    assert tn.arrived(row("Bravo Market", end=MARKET, dest=far)) is False
    assert tn.place_suggestions([row("Bravo Market", end=MARKET, dest=far)], [FOUND], NAMES) == {}, "it ended here, but navigated 400 m away"
    assert tn.point_suggestion(MARKET[0], MARKET[1], [row("Bravo Market", end=MARKET, dest=far)], NAMES) is None


def test_a_destination_inside_the_place_names_it_wherever_the_drive_ended():
    got = tn.place_suggestions([row("Bravo Market", end=HOME, dest=east(MARKET, 60))], PlaceIndex([FOUND]), NAMES)
    assert got[7]["name"] == "Bravo Market"


def test_the_most_frequent_name_wins_and_the_rest_are_alternates():
    rows = [row("Bravo Market", minutes=0), row("Bravo Market", minutes=60, vin=BRAVO), row("Bravo Grocery", minutes=120), row("Market Parking", minutes=180)]
    got = tn.place_suggestions(rows, [FOUND], NAMES)[7]
    assert got["name"] == "Bravo Market" and got["n"] == 2 and got["alternates"] == ["Market Parking", "Bravo Grocery"], "a tie between the others goes to the latest"
    assert got["source"] == "from Alpha's and Bravo's navigation"


def test_a_spot_nobody_named_takes_the_suggestion_for_the_picker():
    got = tn.point_suggestion(*east(MARKET, 50), [row("Bravo Market")], NAMES)
    assert got["name"] == "Bravo Market"
    assert tn.point_suggestion(*HOME, [row("Bravo Market")], NAMES) is None


def test_for_trip_matches_a_drive_cut_a_minute_apart():
    r = row("Bravo Market", minutes=10)
    assert tn.for_trip([r], T0 + timedelta(minutes=9), T0 + timedelta(minutes=30)) is r
    assert tn.for_trip([r], T0 + timedelta(minutes=11), T0 + timedelta(minutes=30)) is r
    assert tn.for_trip([r], T0 + timedelta(minutes=40), T0 + timedelta(minutes=60)) is None


def test_the_live_route_shows_only_while_driving_and_fresh():
    now = T0 + timedelta(minutes=1)
    c = {"Gear": "D", "DestinationName": "Bravo Market, 12 Elm Street", "MinutesToArrival": 7.6, "MilesToArrival": 3.2}
    assert tn.live_route(c, T0, now) == {"name": "Bravo Market", "minutes": 8, "miles": 3.2}
    assert tn.live_route({**c, "Gear": "P"}, T0, now) is None, "parked: no route to show"
    assert tn.live_route(c, T0, T0 + timedelta(minutes=30)) is None, "stale"
    assert tn.live_route({"Gear": "D"}, T0, now) is None, "no route"


def test_the_stream_worker_carries_the_route_for_now():
    import json

    import tesla_live

    w = tesla_live.TeslaStream(connect=None, clock=lambda: T0 + timedelta(minutes=1))
    w.vins = {ALPHA}
    frame = [b"tesla_telemetry_V", json.dumps(payload(0.5, Gear="ShiftStateD", DestinationName="Bravo Market", MinutesToArrival=4.0)).encode()]
    assert w.handle(frame)
    assert w.route(ALPHA) == {"name": "Bravo Market", "minutes": 4, "miles": None}
    w.handle([b"tesla_telemetry_V", json.dumps(payload(0.8, DestinationName=None)).encode()])
    assert w.route(ALPHA) is None, "a cleared route clears what Now shows"

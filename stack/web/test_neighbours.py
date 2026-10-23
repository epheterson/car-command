"""Previous and next: one indexed row each side, and parked stretches found between events."""

from datetime import datetime, timedelta, timezone

import pytest

from neighbours import neighbours, parked_after, parked_before

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def ev(a, b, **kw):
    return dict(kw, start=T0 + a * M, end=None if b is None else T0 + b * M)


# a drive 0–30, a five-minute stop, a charge 35–95, two hours parked, a drive 215–240, still parked
EVENTS = [ev(0, 30, kind="drive"), ev(35, 95, kind="charge"), ev(215, 240, kind="drive")]


def before(t):
    return next((e for e in reversed(EVENTS) if e["start"] < t), None)


def after(t):
    return next((e for e in EVENTS if e["start"] >= t), None)


def test_parked_stretches_are_the_gaps_between_events_long_enough_to_count():
    s = parked_after(after, T0 - 60 * M)
    assert s is not None and s[:2] == (T0 + 95 * M, T0 + 215 * M) and s[2] is EVENTS[1], "the five-minute stop is not a stretch"
    assert parked_after(after, T0 + 95 * M)[:2] == (T0 + 240 * M, None), "after the stretch from 95: the car still parked, a stretch with no end"
    assert parked_before(before, T0 + 240 * M)[:2] == (T0 + 95 * M, T0 + 215 * M)
    assert parked_before(before, T0 + 95 * M) is None, "before the charge there is only the five-minute stop"


def test_an_event_still_going_ends_the_walk():
    global EVENTS
    saved, EVENTS = EVENTS, [ev(0, 30), ev(60, None)]
    try:
        assert parked_after(after, T0 + 30 * M) is None and parked_before(before, T0 + 61 * M)[:2] == (T0 + 30 * M, T0 + 60 * M)
    finally:
        EVENTS = saved


class Conn:
    """Answers the start lookup, then each neighbour query from `rows` by its comparison."""

    def __init__(self, start, rows):
        self.start, self.rows, self.sql = start, rows, []

    def execute(self, sql, args):
        self.sql.append(" ".join(sql.split()))
        row = {"start": self.start} if sql.startswith("SELECT start_date AS start") or sql.startswith("SELECT session_start_date AS start") else None
        if row is None:
            op = "<" if " < %s" in sql else ">"
            row = self.rows.get(("drive" if "FROM trips" in sql else "charge", op))
        return type("R", (), {"fetchone": lambda _self: row})()


def test_a_drives_neighbours_are_one_index_step_each_and_carry_their_label():
    rows = {
        ("drive", "<"): {"id": 6, "start": T0 - 60 * M, "end": T0 - 30 * M, "start_odometer": 100.0, "destination_odometer": 112.5, "lat": 1.0, "lon": 2.0},
        ("drive", ">"): {"id": 8, "start": T0 + 60 * M, "end": None, "start_odometer": 112.5, "destination_odometer": None, "lat": None, "lon": None},
    }
    conn = Conn(T0, rows)
    known = [{"name": "Work", "latitude": 1.0, "longitude": 2.0, "radius_m": 100}]
    n = neighbours(conn, "drive", "V1", 7, known=known)
    assert n["prev"] == {"kind": "drive", "id": 6, "start": T0 - 60 * M, "end": T0 - 30 * M, "where": "Work", "href": "/drives/6", "miles": 12.5}
    assert n["next"]["id"] == 8 and n["next"]["miles"] is None and n["next"]["where"] is None
    asks = conn.sql[1:]
    assert all("ORDER BY t.start_date" in q and q.endswith("LIMIT 1") and "t.vin = %s" in q and "cc_hidden" in q for q in asks), "the (vin, start_date) index, one row"
    assert "DESC" in asks[0] and "ASC" in asks[1]
    assert neighbours(Conn(None, {}), "charge", "V1", 9) == {"prev": None, "next": None}, "not this car's: nothing"
    with pytest.raises(ValueError):
        neighbours(conn, "sleep", "V1", 1)


def test_the_arrows_name_the_date_and_a_short_label():
    from test_parts import ENV

    n = {
        "prev": {"kind": "charge", "id": 4, "start": T0, "end": T0 + M, "where": "Home", "href": "/charges/4", "gained": 43.0},
        "next": {"kind": "parked", "id": 99, "start": T0 + M, "end": None, "where": None, "href": "/parked/99", "duration": None},
    }
    html = ENV.from_string('{% import "_prevnext.html" as pn %}{{ pn.prevnext(n, "/cc", "?vin=V1") }}').render(n=n)
    assert 'rel="prev" href="/cc/charges/4?vin=V1"' in html and "+43% · Home" in html and "parked now" in html and 'rel="next"' in html
    one = ENV.from_string('{% import "_prevnext.html" as pn %}{{ pn.prevnext(n) }}').render(n=dict(n, next=None))
    assert 'class="pn-next pn-none"' in one and 'rel="next"' not in one
    assert ENV.from_string('{% import "_prevnext.html" as pn %}{{ pn.prevnext(n) }}').render(n={"prev": None, "next": None}).strip() == ""
    from pathlib import Path

    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    assert '".prevnext a[rel=" + (e.key === "ArrowLeft" ? "prev" : "next")' in js and "input, textarea, select" in js


def test_a_zero_length_charge_does_not_stop_the_walk():
    global EVENTS
    saved, EVENTS = EVENTS, [ev(0, 30), ev(30, 30), ev(120, 150)]
    try:
        assert parked_after(after, T0)[:2] == (T0 + 30 * M, T0 + 120 * M)
    finally:
        EVENTS = saved

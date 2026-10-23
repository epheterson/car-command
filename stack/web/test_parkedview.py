"""A parked stretch: found from any second inside it, its figures, its bands and readings."""

from datetime import datetime, timedelta, timezone

from neighbours import stretch_at
from parkedview import figures, mode_bands, readings, window

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
M = timedelta(minutes=1)


class Conn:
    """Answers the nearest drive or charge either side of a time from a list of events."""

    def __init__(self, drives, charges=()):
        self.events = {"trips": drives, "charging_sessions": list(charges)}

    def execute(self, sql, args):
        _vin, at = args
        rows = self.events["trips" if "FROM trips" in sql else "charging_sessions"]
        if " < %s" in sql:
            hit = max((e for e in rows if e["start"] < at), key=lambda e: e["start"], default=None)
        else:
            hit = min((e for e in rows if e["start"] > at), key=lambda e: e["start"], default=None)
        return type("R", (), {"fetchone": lambda _self: dict(hit, id=1, lat=39.8, lon=-89.65) if hit else None})()


DRIVES = [{"start": T0 - H, "end": T0}, {"start": T0 + 4 * H, "end": T0 + 5 * H}, {"start": T0 + 5 * H + 5 * M, "end": T0 + 6 * H}]


def test_any_second_inside_a_stretch_finds_its_start_and_end():
    conn = Conn(DRIVES)
    assert stretch_at(conn, "V", T0)[:2] == (T0, T0 + 4 * H), "its own first second"
    assert stretch_at(conn, "V", T0 + 2 * H)[:2] == (T0, T0 + 4 * H), "a Day row cut at midnight links from the middle"
    assert stretch_at(conn, "V", T0 - 30 * M) is None, "inside a drive"
    assert stretch_at(conn, "V", T0 + 5 * H + 2 * M) is None, "a five-minute stop is no stretch"
    assert stretch_at(conn, "V", T0 + 7 * H)[:2] == (T0 + 6 * H, None), "parked still: no end"
    assert stretch_at(Conn(DRIVES), "V", T0 - 2 * H) is None, "before anything was logged"


def test_the_figures_read_both_ends_and_price_the_loss():
    levels = [(T0 - H, 80.0), (T0, 80.0), (T0 + 2 * H, 79.0), (T0 + 4 * H, 90.0)]  # the next charge's first reading at the end
    ranges = [(T0 - H, 240.0), (T0 + 2 * H, 237.0), (T0 + 4 * H, 270.0)]
    states = [(T0, T0 + H, "PARKED"), (T0 + H, T0 + 4 * H, "OFFLINE")]
    modes = [(T0, T0 + 30 * M, (True, False)), (T0 + 30 * M, T0 + 40 * M, (False, True))]
    f = figures(T0, T0 + 4 * H, levels, ranges, [(T0, 70.0), (T0 + 3 * H, 60.0)], [(T0, 55.0)], states, modes, 0.25, 0.20)
    assert (f["level_start"], f["level_end"], f["drop"]) == (80.0, 79.0, 1.0), "the end is read just before the next event"
    assert f["range_lost"] == 3.0 and f["kwh_lost"] == 0.75 and round(f["cost"], 2) == 0.15 and f["per_hour"] == 0.75
    assert f["asleep_share"] == 0.75 and round(f["sentry_h"], 2) == 0.5 and round(f["climate_h"], 2) == round(10 / 60, 2)
    assert f["ran"] == "idle" and f["inside"] == (70.0, 60.0) and f["outside"] == (55.0, 55.0)
    assert f["duration"] == 4 * H and f["sentry"] == 30 * M
    guarded = figures(T0, T0 + H, levels, ranges, [], [], [], [(T0, T0 + H, (True, False))], None, None)
    assert guarded["ran"] == "sentry" and guarded["kwh_lost"] is None and guarded["cost"] is None


def test_bands_and_readings_stay_inside_the_stretch():
    modes = [(T0 - H, T0 + H, (True, False)), (T0 + 2 * H, T0 + 3 * H, (False, True)), (T0 + 5 * H, T0 + 6 * H, (True, True))]
    assert mode_bands(modes, T0, T0 + 4 * H, "Sentry") == [[T0, T0 + H, "sentry", "Sentry on"], [T0 + 2 * H, T0 + 3 * H, "climate", "Climate on"]]
    levels = [(T0 - H, 80.0), (T0 + H, 79.0), (T0 + 2 * H, 79.0), (T0 + 4 * H, 90.0)]
    rows = readings(T0, T0 + 4 * H, levels, [], [], [], [(T0, T0 + 4 * H, "OFFLINE")])
    assert [(r["at"], r["level"]) for r in rows] == [(T0, 80.0), (T0 + H, 79.0)], "a row a change, none at the next event's start"
    assert all(r["asleep"] for r in rows)


def test_a_window_holds_one_point_at_its_start_not_every_earlier_one():
    series = [(T0 - 3 * H, 50.0), (T0 - H, 60.0), (T0 + H, 70.0), (T0 + 5 * H, 99.0)]
    assert window(series, T0, T0 + 4 * H) == [(T0, 60.0), (T0 + H, 70.0)]
    assert window([], T0, T0 + H) == []


def test_a_stretch_that_began_partway_through_a_second_is_found_from_its_link():
    from parkedview import moment

    started = T0 + timedelta(microseconds=600_000)  # a drive that ended at 12:00:00.6
    conn = Conn([{"start": T0 - H, "end": started}, {"start": T0 + 4 * H, "end": T0 + 5 * H}])
    ref = int(started.timestamp())  # what every parked link carries
    span = stretch_at(conn, "V", moment(ref))
    assert span is not None and span[0] == started and int(span[0].timestamp()) == ref, "the link's whole second holds the start"
    assert moment(10**20) is None and moment(-(10**20)) is None, "a number no clock reaches is no stretch, not an error"

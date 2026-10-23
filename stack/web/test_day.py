from datetime import datetime, timedelta, timezone

from day import asleep_share, day_sessions, day_totals, level_lookup, since_charge

T = lambda h, m=0: datetime(2026, 9, 4, h, m, tzinfo=timezone.utc)
START, END = T(0), T(23, 59)


def test_day_reads_in_order_with_parked_stretches_between():
    drives = [
        {"id": 1, "start_date": T(8), "destination_date": T(8, 40), "from": "Home", "to": "Work", "miles": 20.0, "duration": timedelta(minutes=40), "level_start": 80.0, "level_end": 70.0},
        {"id": 2, "start_date": T(17), "destination_date": T(17, 45), "from": "Work", "to": "Home", "miles": 21.0, "duration": timedelta(minutes=45), "level_start": 68.0, "level_end": 57.0},
    ]
    charges = [
        {
            "id": 3,
            "session_start_date": T(22),
            "session_end_date": T(23, 30),
            "where": "Home",
            "kwh": 20.0,
            "cost": 8.0,
            "start_level": 56.0,
            "end_level": 82.0,
            "duration": timedelta(minutes=90),
            "charging_type": "AC",
        }
    ]
    states = [
        (T(0), T(8), "OFFLINE"),
        (T(8), T(8, 40), "DRIVING"),
        (T(8, 40), T(9, 10), "PARKED"),
        (T(9, 10), T(16, 55), "OFFLINE"),
        (T(16, 55), T(17, 45), "DRIVING"),
        (T(17, 45), T(23, 59), "PARKED"),
    ]
    levels = [(T(0), 81.0), (T(8), 80.0), (T(8, 40), 70.0), (T(9, 10), 69.0), (T(17), 68.0), (T(17, 45), 57.0), (T(22), 56.0), (T(23, 30), 82.0)]
    rows = day_sessions(drives, charges, states, levels, START, END)
    # the night is one sleep (the 10-minute awake tail folds in); the work day is idle 30 min then asleep; evening idle
    assert [(r["kind"], r.get("sub")) for r in rows] == [
        ("parked", "sleep"),
        ("drive", None),
        ("parked", "idle"),
        ("parked", "sleep"),
        ("drive", None),
        ("parked", "idle"),
        ("charge", None),
        ("parked", "idle"),
    ]
    night = rows[0]
    assert night["duration"] == timedelta(hours=8) and night["asleep_share"] == 1.0 and night["drop"] == 1.0
    work_idle, work_sleep = rows[2], rows[3]
    assert work_idle["start"] == T(8, 40) and work_idle["end"] == T(9, 10) and work_sleep["end"] == T(17)  # the 5-minute wake before the drive folds into the sleep
    tot = day_totals(rows, 75.0)
    assert tot["drives"] == 2 and tot["miles"] == 41.0 and round(tot["used_kwh"], 2) == 15.75 and round(tot["wh_per_mi"]) == 384
    assert tot["charges"] == 1 and tot["kwh"] == 20.0 and round(tot["parked_h"], 1) == 21.1


def test_short_gaps_are_not_parked_rows_and_lookups_are_stepwise():
    drives = [{"id": 1, "start_date": T(8), "destination_date": T(8, 30), "miles": 5.0}, {"id": 2, "start_date": T(8, 35), "destination_date": T(9), "miles": 6.0}]
    rows = day_sessions(drives, [], [], [(T(7), 50.0)], T(7, 55), T(9))
    assert [r["kind"] for r in rows] == ["drive", "drive"]
    at = level_lookup([(T(1), 10.0), (T(2), 20.0)])
    assert at(T(0, 30)) is None and at(T(1, 30)) == 10.0 and at(T(2)) == 20.0
    assert asleep_share([], T(0), T(1)) is None


def test_parked_rows_carry_range_loss_cabin_temperature_and_modes():
    drives = [{"id": 1, "start_date": T(8), "destination_date": T(8, 40), "miles": 20.0, "duration": timedelta(minutes=40), "level_start": 80.0, "level_end": 70.0}]
    states = [(T(0), T(8), "OFFLINE")]
    levels = [(T(0), 81.0), (T(8), 80.0), (T(8, 40), 70.0)]
    ranges = [(T(0), 200.0), (T(8), 197.5)]
    inside = [(T(0), 60.0), (T(7), 68.0)]
    modes = [(T(0), T(3), (True, False)), (T(3), T(8), (False, True))]
    rows = day_sessions(drives, [], states, levels, T(0), T(9), ranges, inside, modes, kwh_per_rated_mi=0.25)
    night = rows[0]
    assert night["sub"] == "sleep"
    assert night["kind"] == "parked" and night["range_loss"] == 2.5 and round(night["kwh_loss"], 3) == 0.625
    # asleep all night: the mode runs are the evening's values carried over, and a sleeping car runs neither
    assert night["inside_start"] == 60.0 and night["inside_end"] == 68.0 and night["sentry_h"] == 0.0 and night["climate_h"] == 0.0
    tot = day_totals(rows, 75.0, 0.25)
    assert tot["parked_range_loss"] == 2.5 and round(tot["phantom_mi_per_h"], 2) == 0.30 and tot["sentry_h"] == 0.0 and tot["battery_used"] == 11.0  # 1 parked + 10 drive
    assert tot["asleep_h"] == 8.0 and tot["idle_h"] == 0.0


def test_sentry_and_climate_count_only_while_the_car_is_awake():
    # one run from 0 to 8 because the value was the same at the last reading before sleep and the first after
    states = [(T(0), T(2), "ONLINE"), (T(2), T(7), "OFFLINE"), (T(7), T(8), "ONLINE")]
    modes = [(T(0), T(8), (True, True))]
    rows = day_sessions([], [], states, [(T(0), 80.0)], T(0), T(8), None, None, modes)
    assert [(r["sub"], r["sentry_h"], r["climate_h"]) for r in rows] == [("idle", 2.0, 2.0), ("sleep", 0.0, 0.0), ("idle", 1.0, 1.0)]
    assert day_totals(rows, 75.0)["sentry_h"] == 3.0


def test_asleep_time_is_unknown_when_no_state_says_so():
    rows = day_sessions([], [], [], [(T(0), 80.0)], T(0), T(8))
    tot = day_totals(rows, 75.0)
    assert tot["parked_h"] == 8.0 and tot["asleep_h"] is None and tot["idle_h"] is None


def test_since_charge_trusts_a_reading_only_when_the_car_has_not_moved_since():
    t0, now = T(1), T(23)
    seen = lambda v, t: (v, t)
    # a reading after the last drive: the battery used is the charge's own end level minus it
    f = since_charge(t0, now, 90.0, seen(88.0, T(1)), seen(60.0, T(20)), seen(280.0, T(1)), seen(190.0, T(20)), 72.0, T(19), 0.25)
    assert f["battery_used"] == 30.0 and f["rated_used"] == 90.0 and f["miles"] == 72.0 and round(f["efficiency"], 2) == 0.8 and f["kwh"] == 22.5 and f["ago"] == now - t0
    # the only reading is from before the drives (a day filled from drive and charge history): not 0% used, unknown
    f = since_charge(t0, now, 90.0, seen(88.0, T(1)), seen(88.0, T(1)), seen(280.0, T(1)), seen(280.0, T(1)), 72.0, T(19), 0.25)
    assert f["battery_used"] is None and f["rated_used"] is None and f["efficiency"] is None and f["kwh"] is None and f["miles"] == 72.0
    # no end level on the charge: the reading that held at its end, if it was still seen then
    f = since_charge(t0, now, None, seen(87.0, T(0, 50)), seen(60.0, T(20)), None, None, 72.0, T(19), None)
    assert f["battery_used"] == 27.0 and f["rated_used"] is None
    f = since_charge(t0, now, None, seen(87.0, T(0, 10)), seen(60.0, T(20)), None, None, 72.0, T(19), None)
    assert f["battery_used"] is None, "a reading last seen 50 minutes before the charge ended predates it"
    # parked since the charge, nothing new logged: the level held
    f = since_charge(t0, now, 90.0, seen(90.0, T(2)), seen(90.0, T(2)), None, None, 0.0, None, None)
    assert f["battery_used"] == 0.0


def test_elevation_profile_in_feet_ignores_jitter():
    from day import elevation_profile

    track = [{"at": T(8, i), "elevation": e} for i, e in enumerate([100.0, 100.5, 110.0, 105.0, 105.5, 130.0, None, 90.0])]
    series, climb, descent = elevation_profile(track)
    assert len(series) == 7 and round(series[0][1]) == 328
    assert round(climb) == round(30 * 3.28084) and round(descent) == round(40 * 3.28084), "100→130 up (the 5 m dip is noise), 130→90 down"
    # a metre a reading is under the noise floor each step, and still a 20 m climb
    _s, climb, descent = elevation_profile([{"at": T(8, i), "elevation": 100.0 + i} for i in range(21)])
    assert round(climb) == round(20 * 3.28084) and descent == 0


def test_time_at_places_follows_the_drives():
    from day import time_at_places

    rows = [
        {"kind": "parked", "start": T(0), "end": T(8), "title": "Parked"},
        {"kind": "drive", "start": T(8), "end": T(8, 30), "title": "Home → Work"},
        {"kind": "parked", "start": T(8, 30), "end": T(17), "title": "Parked"},
        {"kind": "drive", "start": T(17), "end": T(17, 30), "title": "Work → Home"},
        {"kind": "parked", "start": T(17, 30), "end": T(23, 59), "title": "Parked"},
    ]
    out = time_at_places(rows, T(0), T(23, 59))
    d = {x["place"]: round(x["hours"], 2) for x in out}
    assert d["Home"] == round(8 + 6.48, 2) and d["Work"] == 8.5 and d["Driving"] == 1.0 and out[-1]["place"] == "Driving"


def test_split_parked_alternates_and_folds_short_pieces():
    from day import split_parked

    states = [(T(0), T(3), "OFFLINE"), (T(3), T(3, 5), "PARKED"), (T(3, 5), T(7), "OFFLINE"), (T(7), T(8), "PARKED")]
    assert split_parked(states, T(0), T(8)) == [(T(0), T(7), "sleep"), (T(7), T(8), "idle")]  # the 5-minute blip folds in
    assert split_parked([], T(0), T(1)) == [(T(0), T(1), "idle")]


def test_drive_rows_carry_the_cabin_temperature_too():
    """The parked rows have said what the cabin was doing since the Day page shipped; the
    drive between them did not, though it reads the same series."""
    drives = [{"id": 1, "start_date": T(8), "destination_date": T(8, 40), "miles": 20.0, "duration": timedelta(minutes=40), "level_start": 80.0, "level_end": 70.0}]
    inside = [(T(0), 60.0), (T(7), 68.0), (T(8, 30), 72.0)]
    rows = day_sessions(drives, [], [(T(0), T(8), "OFFLINE")], [(T(8), 80.0)], T(0), T(9), [], inside, [], kwh_per_rated_mi=0.25)
    drive = next(r for r in rows if r["kind"] == "drive")
    assert drive["inside_start"] == 68.0 and drive["inside_end"] == 72.0
    rows = day_sessions(drives, [], [(T(0), T(8), "OFFLINE")], [(T(8), 80.0)], T(0), T(9), [], [], [], kwh_per_rated_mi=0.25)
    assert next(r for r in rows if r["kind"] == "drive")["inside_start"] is None, "no cabin series, no number — not a zero"


def test_every_parked_row_knows_where_the_car_was():
    drives = [
        {"id": 1, "start_date": T(8), "destination_date": T(8, 40), "from": "Home", "to": "Work", "miles": 20.0, "duration": timedelta(minutes=40)},
        {"id": 2, "start_date": T(17), "destination_date": T(17, 45), "from": "Work", "to": "?", "miles": 21.0, "duration": timedelta(minutes=45)},
    ]
    charges = [
        {"id": 3, "session_start_date": T(19), "session_end_date": T(20), "where": None, "kwh": 5.0, "duration": timedelta(hours=1)},
        {"id": 4, "session_start_date": T(21), "session_end_date": T(22), "where": "Grocer", "kwh": 5.0, "duration": timedelta(hours=1)},
    ]
    rows = day_sessions(drives, charges, [], [(T(0), 80.0)], T(0), T(23, 59))
    parked = [r["where"] for r in rows if r["kind"] == "parked"]
    assert parked == ["Home", "Work", None, None, "Grocer"], "overnight at the first drive's start; after a drive its end; an unknown end stays unknown through a charge with no place"
    rows = day_sessions([], charges[1:], [], [(T(0), 80.0)], T(0), T(23, 59))
    assert [r["where"] for r in rows if r["kind"] == "parked"] == ["Grocer", "Grocer"], "no drive: the car was where it charged"


def test_parked_rows_carry_their_place_for_the_naming_card():
    """A parked row carries the place the car sat at as the card opens on it (refs): from the
    drive that ended there, or the charge, named or not; nothing when nothing says where."""
    home = {"name": "Home", "lat": 39.79, "lon": -89.66, "new": False}
    elm = {"name": "Elm Street, Springfield", "lat": 39.80, "lon": -89.65, "new": True}
    grocer = {"name": "Grocer", "lat": 39.81, "lon": -89.64, "new": False}
    drives = [
        {
            "id": 1,
            "start_date": T(8),
            "destination_date": T(8, 40),
            "from": "Home",
            "to": "Elm Street, Springfield",
            "from_ref": home,
            "to_ref": elm,
            "miles": 2.0,
            "duration": timedelta(minutes=40),
        }
    ]
    charges = [{"id": 3, "session_start_date": T(19), "session_end_date": T(20), "where": "Grocer", "where_ref": grocer, "kwh": 5.0, "duration": timedelta(hours=1)}]
    rows = day_sessions(drives, charges, [], [(T(0), 80.0)], T(0), T(23, 59))
    parked = [r for r in rows if r["kind"] == "parked"]
    assert [p["where"] for p in parked] == ["Home", "Elm Street, Springfield", "Grocer"]
    assert [p["refs"] for p in parked] == [[home], [elm], [grocer]]
    assert [r["refs"] for r in rows if r["kind"] == "drive"] == [[home, elm]] and [r["refs"] for r in rows if r["kind"] == "charge"] == [[grocer]]
    rows = day_sessions([dict(drives[0], to="?", to_ref=None)], [], [], [(T(0), 80.0)], T(0), T(23, 59))
    assert [r["refs"] for r in rows if r["kind"] == "parked"] == [[home], []], "an unknown end: nothing to tap"


def test_what_a_charge_bought_prices_the_miles_it_paid_for():
    from day import bought

    b = {"miles": 120.0, "battery_used": 40.0, "rated_used": 150.0, "kwh": 36.0, "efficiency": 0.8}
    got = bought(b, cost=6.0, kwh_charged=40.0, capacity_kwh=80.0)
    assert got["used_kwh"] == 36.0 and got["wh_per_mi"] == 300.0, "rated range × kWh a rated mile first"
    assert round(got["cost_per_mi"], 4) == round(0.15 * 36 / 120, 4), "this charge's price a kWh × what the miles used"
    got = bought(dict(b, kwh=None), cost=None, kwh_charged=40.0, capacity_kwh=80.0)
    assert got["used_kwh"] == 32.0 and got["cost_per_mi"] is None, "no rated figure: the battery points × the pack; no price, no cost a mile"
    assert bought(dict(b, miles=0.4), 6.0, 40.0, 80.0)["wh_per_mi"] is None, "under a mile driven is no efficiency"


def test_gps_height_noise_is_not_a_climb_and_a_real_hill_is():
    from day import elevation_profile

    flat = [{"at": T(8, i % 60), "elevation": 200.0 + (4 if i % 2 else -4)} for i in range(120)]
    _s, climb, descent = elevation_profile(flat)
    assert climb < 5 and descent < 5, "±4 m of noise on a flat road is no climb"
    hill = [{"at": T(8, i % 60), "elevation": 100.0 + min(i, 100) + (3 if i % 2 else -3)} for i in range(130)]
    _s, climb, descent = elevation_profile(hill)
    assert abs(climb / 3.28084 - 100) <= 8 and descent / 3.28084 <= 8, "a 100 m hill climbs about 100 m, noise and all"


def test_a_stretch_driven_out_of_reach_is_not_parked_time_or_phantom_drain():
    t = datetime(2026, 10, 15, 23, 16, tzinfo=timezone.utc)
    rows = [
        {"kind": "parked", "start": t, "end": t + timedelta(minutes=81), "duration": timedelta(minutes=81), "range_loss": 6.0, "moved_mi": 5.0},
        {"kind": "parked", "start": t + timedelta(hours=2), "end": t + timedelta(hours=4), "duration": timedelta(hours=2), "range_loss": 0.5},
    ]
    tot = day_totals(rows, 75.0)
    assert tot["parked_h"] == 2.0 and tot["parked_range_loss"] == 0.5


def test_a_charge_over_midnight_is_shown_both_days_and_counted_on_the_first():
    from datetime import datetime, timedelta, timezone

    import day

    d0 = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)  # local midnight, Pacific
    c = {"id": 9, "session_start_date": d0 - timedelta(hours=3), "session_end_date": d0 + timedelta(hours=6), "kwh": 61.8, "cost": 19.43}
    rows = day.day_sessions([], [c], [], [], d0, d0 + timedelta(days=1))
    (row,) = [r for r in rows if r["kind"] == "charge"]
    assert row["carried"] is True
    totals = day.day_totals(rows, 75.0)
    assert totals["kwh"] == 0 and totals["cost"] == 0 and totals["charges"] == 0
    before = day.day_sessions([], [c], [], [], d0 - timedelta(days=1), d0)
    assert day.day_totals(before, 75.0)["kwh"] == 61.8

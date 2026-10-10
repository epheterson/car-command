from datetime import datetime, timedelta, timezone

import pytest

from samples import run_points, align, as_runs, change_points, charge_samples, drive_samples, in_force, thinned, with_measured_power

T = datetime(2026, 9, 11, 17, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def test_a_reading_holds_until_the_next_one():
    """The interval tables keep a run per value, not a value per minute, so between runs
    the answer is the last reading rather than a hole in the table."""
    runs = [(at(0), at(3), 80.0), (at(5), at(9), 79.0)]
    assert in_force([at(0), at(1), at(4), at(6), at(20)], runs) == [80.0, 80.0, 80.0, 79.0, 79.0]


def test_before_the_first_reading_there_is_no_answer():
    """And saying so is better than borrowing a number from the future."""
    assert in_force([at(-5), at(0)], [(at(0), at(3), 80.0)]) == [None, 80.0]
    assert in_force([at(0)], []) == [None]


def test_align_lines_up_every_series_against_one_clock():
    rows = align([at(0), at(6)], level=[(at(0), at(9), 80.0)], rated=[(at(0), at(4), 244.0), (at(5), at(9), 240.0)])
    assert rows == [{"level": 80.0, "rated": 244.0}, {"level": 80.0, "rated": 240.0}]


def test_a_drive_row_carries_the_fix_and_the_readings_around_it():
    track = [
        {"at": at(0), "speed": 0.0, "level": 80.0, "odometer": 1000.0, "elevation": 400.0, "latitude": 1.0, "longitude": 2.0},
        {"at": at(2), "speed": 31.0, "level": 79.0, "odometer": 1001.1, "elevation": 420.0, "latitude": 1.1, "longitude": 2.1},
    ]
    rows = drive_samples(track, rated=[(at(0), at(1), 244.0), (at(2), at(9), 241.0)], outside=[(at(0), at(9), 68.0)])
    assert [r["speed"] for r in rows] == [0.0, 31.0]
    assert [r["rated"] for r in rows] == [244.0, 241.0]
    assert rows[1]["outside"] == 68.0 and rows[1]["odometer"] == 1001.1


def test_a_drive_with_no_track_has_no_rows():
    """Older history has drive endpoints and no route; an empty table is the honest answer."""
    assert drive_samples([], rated=[(at(0), at(9), 244.0)]) == []


def test_a_charge_row_is_a_moment_something_changed():
    powers = [(at(0), at(1), 6.6), (at(1), at(9), 6.5)]
    rows = charge_samples(at(0), at(9), powers, level=[(at(0), at(4), 40.0), (at(5), at(9), 42.0)], volts=[(at(0), at(9), 240.0)])
    assert rows == [
        {"at": at(0), "kw": 6.6, "level": 40.0, "volts": 240.0},
        {"at": at(1), "kw": 6.5, "level": 40.0, "volts": 240.0},
        {"at": at(5), "kw": 6.5, "level": 42.0, "volts": 240.0},
    ]


def test_a_steady_charge_is_not_one_row():
    """charging_powers keeps a run per constant value, so an overnight charge at a flat
    6.6 kW is a single run. Built on that clock the table had one row, for a session that
    reported a new battery level every few minutes all night."""
    powers = [(at(0), at(300), 6.6)]
    levels = [(at(0), at(60), 40.0), (at(60), at(120), 45.0), (at(120), at(300), 50.0)]
    rows = charge_samples(at(0), at(300), powers, level=levels)
    assert [r["level"] for r in rows] == [40.0, 45.0, 50.0]
    assert all(r["kw"] == 6.6 for r in rows)


def test_a_session_starts_at_its_start():
    """Not at whenever the first reading happened to land."""
    assert change_points([[(at(3), at(9), 1.0)]], at(0), at(9)) == [at(0), at(3)]
    assert change_points([[(at(-5), at(9), 1.0)]], at(0), at(9)) == [at(0)], "a run that began before the window is not a change inside it"


def test_a_long_drive_is_thinned_and_says_so():
    """Twenty-second sampling turns a road trip into thousands of rows. Every nth keeps
    the shape; the count it stands for is what makes the gaps readable rather than
    look like missing data."""
    rows = [{"i": i} for i in range(1000)]
    out, step = thinned(rows, 200)
    assert step == 5 and len(out) == 200
    assert out[0]["i"] == 0 and out[-1]["i"] == 999, "the ends of the drive are the two rows you must not drop"


def test_a_short_drive_is_left_alone():
    rows = [{"i": i} for i in range(12)]
    assert thinned(rows, 200) == (rows, 1)
    assert thinned(rows, 0) == (rows, 1)


def test_volts_times_amps_stands_in_where_the_car_reported_no_power():
    """918 of 4,143 finished charges read 0.0 kWh added, on a page that also said the
    battery went 19 % to 83 %. Both Teslas stop reporting charger_power partway through
    their history; volts and amps carry on."""
    powers = [(at(0), at(60), 0.0)]
    electrics = [(at(0), at(60), 234.0, 48.0)]
    assert with_measured_power(powers, electrics) == [(at(0), at(60), 234.0 * 48.0 / 1000.0)]


def test_a_session_the_car_does_report_is_left_alone():
    """Volts times amps is close but not the same number — 0.985 of it on median — so it
    must not overwrite a reading that exists."""
    powers = [(at(0), at(60), 11.5)]
    assert with_measured_power(powers, [(at(0), at(60), 234.0, 48.0)]) == [(at(0), at(60), 11.5)]
    assert with_measured_power(powers, []) == powers


def test_the_stand_in_applies_only_to_the_stretch_that_is_missing():
    powers = [(at(0), at(30), 11.5), (at(30), at(60), 0.0)]
    out = with_measured_power(powers, [(at(0), at(60), 234.0, 48.0)])
    assert out == [(at(0), at(30), 11.5), (at(30), at(60), 11.232)]


def test_a_parked_car_is_not_charging():
    """Residual bus voltage and a stray amp would otherwise turn an idle night into
    energy nobody drew."""
    assert with_measured_power([(at(0), at(60), 0.0)], [(at(0), at(60), 12.0, 0.2)]) == [(at(0), at(60), 0.0)]
    assert with_measured_power([(at(0), at(60), 0.0)], [(at(0), at(60), None, 48.0)]) == [(at(0), at(60), 0.0)]


def test_zero_stretches_survive():
    """charging_powers writes them so that a run can never bridge two sessions."""
    out = with_measured_power([(at(0), at(10), 0.0), (at(10), at(20), 6.6)], [(at(0), at(5), 234.0, 48.0)])
    assert [round(kw, 3) for _f, _l, kw in out] == [11.232, 0.0, 6.6]


def test_instant_readings_are_held_until_the_next_one():
    """cc_charging_electrics keeps first_date equal to last_date, because a voltage that
    drifts a tenth of a volt a minute never repeats. Integrating rows of zero length is
    how a four-hour charge at a steady 235 V and 48 A came out as 0.9 kWh."""
    instants = [(at(0), at(0), 11.2), (at(1), at(1), 11.3), (at(2), at(2), 11.2)]
    assert as_runs(instants) == [(at(0), at(1), 11.2), (at(1), at(2), 11.3), (at(2), at(7), 11.2)]


def test_a_gap_is_a_gap_not_energy():
    """Unplugged at noon and plugged back in at four is not four hours of charging."""
    held = as_runs([(at(0), at(0), 11.2), (at(240), at(240), 11.2)])
    assert held[0] == (at(0), at(5), 11.2), "one reading describes five minutes, not four hours"


def test_a_steady_charge_integrates_to_what_the_car_metered():
    """The real session this came from: 238 minute-by-minute readings at about 235 V and
    48 A over four hours, with the car's own meter reading 42.58 kWh. A charger delivers
    a little more than the battery takes, so the two agree to within a few percent."""
    volts_amps = [(at(i), at(i), 235.0, 48.0) for i in range(238)]
    runs = with_measured_power([(at(-100), at(400), 0.0)], volts_amps)
    kwh = sum(kw * (last - first).total_seconds() / 3600 for first, last, kw in runs)
    assert 41 < kwh < 46, kwh


def test_a_drive_row_carries_the_power_reading():
    """The export has had a kW column all along that nothing mapped."""
    track = [{"at": at(0), "speed": 62.0, "power_kw": 17.0, "level": 80.0, "odometer": 1000.0, "elevation": 400.0, "latitude": 1.0, "longitude": 2.0}]
    assert drive_samples(track)[0]["power_kw"] == 17.0


def test_run_points_clip_to_the_window_and_step_across_gaps():
    t = lambda h, m=0: datetime(2026, 9, 10, tzinfo=timezone.utc) + timedelta(hours=h, minutes=m)
    runs = [(t(0), t(16), 0.0), (t(16), t(20), 10.4), (t(20), t(20), 0.0), (t(31), t(33), 10.5)]
    pts = run_points(runs, t(16), t(33, 30))
    assert pts[0] == (t(16), 0.0), "a run from before the window starts at the window, not in July"
    assert (t(31), 0.0) in pts and (t(31), 10.5) in pts, "the unreported night holds the last value, then steps"
    assert all(t(16) <= at <= t(33, 30) for at, _ in pts)
    assert run_points([(t(1), t(2), 5.0)], t(3), t(4)) == []


def test_reported_power_instants_hold_until_the_next_reading():
    """The Lucid's power drifts every poll, so its rows never form runs: each is an instant, and
    a charge made only of instants said 0.0 kWh. Held to the next reading they integrate."""
    t = lambda m: T + timedelta(minutes=m)
    held = with_measured_power([(t(0), t(0), 10.4), (t(3), t(3), 10.5), (t(6), t(6), 0.0)], [])
    assert [(a, b) for a, b, _ in held] == [(t(0), t(3)), (t(3), t(6)), (t(6), t(11))]
    assert sum(kw * (b - a).total_seconds() / 3600 for a, b, kw in held) == pytest.approx((10.4 + 10.5) * 3 / 60)

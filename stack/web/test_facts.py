"""Facts are computed once; these pin the pure parts."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from facts import charge_facts
from stats import month_window

PT = ZoneInfo("America/Los_Angeles")
pytestmark = pytest.mark.usefixtures("springfield_plan")
T0 = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)  # 23:00 PDT Aug 9, off-peak


def test_charge_facts_integrate_only_overlapping_power_and_price_home_only():
    powers = [
        (T0 - timedelta(hours=5), T0 - timedelta(hours=4), 7.0),  # earlier session, must not count
        (T0, T0 + timedelta(hours=1), 10.0),
        (T0 + timedelta(hours=1), T0 + timedelta(hours=3), 5.0),
        (T0 + timedelta(hours=3), T0 + timedelta(hours=4), 0.0),
    ]
    sessions = [
        {"id": 7, "session_start_date": T0, "session_end_date": T0 + timedelta(hours=2), "session_position_latitude": 1.0, "session_position_longitude": 1.0},
        {"id": 8, "session_start_date": T0 + timedelta(hours=2), "session_end_date": T0 + timedelta(hours=3), "session_position_latitude": 9.0, "session_position_longitude": 9.0},
    ]
    out = charge_facts(sessions, powers, PT, lambda lat, lon: lat == 1.0)
    assert [(i, round(k, 2), h) for i, k, _c, _p, h, *_ in out] == [(7, 15.0, True), (8, 5.0, False)]
    assert round(out[0][2], 2) == 6.0 and out[0][3] == "Springfield EV"  # 15 kWh off-peak at $0.40
    assert out[1][2] is None


def test_the_cars_meter_wins_and_prices_by_the_power_profile():
    powers = [(T0, T0 + timedelta(hours=1), 10.0)]
    s = {"id": 1, "session_start_date": T0, "session_end_date": T0 + timedelta(hours=1), "session_position_latitude": 1.0, "session_position_longitude": 1.0, "real_charged": 12.0}
    ((_i, kwh, cost, _p, home, *_),) = charge_facts([s], powers, PT, lambda lat, lon: True)
    assert kwh == 12.0 and round(cost, 2) == 4.80 and home  # 12 kWh at $0.40, not 10
    # no power readings at all (Bravo from 2025): flat profile over the session
    ((_i, kwh, cost, _p, _h, *_),) = charge_facts([dict(s, real_charged=20.0)], [], PT, lambda lat, lon: True)
    assert kwh == 20.0 and round(cost, 2) == 8.00


def test_month_window_defaults_to_the_newest_row_and_links_neighbours():
    w = month_window(None, PT, latest=datetime(2026, 7, 31, 23, 30, tzinfo=timezone.utc))  # 16:30 PDT July 31
    assert w["ym"] == "2026-07" and w["prev"] == "2026-06" and w["next"] == "2026-08"
    assert w["start"].isoformat() == "2026-07-01T00:00:00-07:00" and w["end"].isoformat() == "2026-08-01T00:00:00-07:00"
    assert month_window("garbage", PT, latest=None)["ym"] == datetime.now(PT).strftime("%Y-%m")
    assert month_window("2025-12", PT)["next"] == "2026-01" and month_window("2025-12", PT)["label"] == "December 2025"


def test_calendar_grid_is_rectangular_and_sums_per_local_day():
    from stats import calendar_grid

    w = month_window("2026-09", PT)
    drives = [
        {"start_date": datetime(2026, 9, 1, 6, 30, tzinfo=timezone.utc), "miles": 10.0},  # Aug 31 23:30 PDT: previous month
        {"start_date": datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc), "miles": 26.3},
        {"start_date": datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc), "miles": 4.0},
    ]
    charges = [{"session_start_date": datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc), "kwh": 30.0, "at_home": True}]
    weeks = calendar_grid(drives, charges, w, PT)
    assert all(len(week) == 7 for week in weeks) and weeks[0][0]["date"].isoformat() == "2026-08-31" and weeks[0][0]["other"]
    assert weeks[0][0]["miles"] == 10.0  # counted on its local day even though that day is outside the month
    day4 = next(c for week in weeks for c in week if c["date"].isoformat() == "2026-09-04")
    assert day4["drives"] == 2 and round(day4["miles"], 1) == 30.3 and day4["charges"] == 1 and day4["home_kwh"] == 30.0
    assert weeks[-1][-1]["date"].isoformat() == "2026-10-04"


def test_efficiency_bins_by_temperature_and_speed():
    from stats import efficiency_bins

    mk = lambda temp, miles, minutes, drop: {"temp_f": temp, "miles": miles, "duration": timedelta(minutes=minutes), "level_start": 80.0, "level_end": 80.0 - drop}
    drives = [mk(45, 20, 40, 12)] * 3 + [mk(75, 20, 40, 8)] * 3 + [mk(75, 0.5, 2, 1)] + [mk(None, 20, 40, 8)] + [mk(75, 20, 15, 8)] * 2
    bins = efficiency_bins(drives, 75.0)
    temp = {(b["lo"], b["hi"]): round(b["wh_per_mi"]) for b in bins["temp"]}
    assert temp == {(40, 50): 450, (70, 80): 300}  # 75 kWh x 12 % / 20 mi = 450 Wh/mi; the short and the unknown-temp drives are out
    assert [b["share"] for b in bins["temp"]] == [1.0, 0.15], "the bars span the rows' own range: the least is a stub, not a third of the most"
    speed = {(b["lo"], b["hi"]): b["drives"] for b in bins["speed"]}
    assert speed == {(25, 35): 7}  # 20 mi in 40 min = 30 mph; the 80 mph pair is under the 3-drive floor
    assert efficiency_bins(drives, None) == {"temp": [], "speed": []}


def test_timeline_threads_every_car_newest_first_by_local_day():
    from stats import timeline_days

    t = lambda h: datetime(2026, 9, 5, h, 0, tzinfo=timezone.utc)
    drives = [
        {"id": 1, "start_date": t(5), "vin": "A", "car_name": "Bravo", "from": "Home", "to": "Work", "miles": 20.0, "duration": timedelta(minutes=30)},  # Sep 4 22:00 PDT
        {"id": 2, "start_date": t(16), "vin": "B", "car_name": "Charlie", "from": "Work", "to": "Home", "miles": 21.0, "duration": timedelta(minutes=35)},
    ]  # Sep 5 09:00 PDT
    charges = [{"id": 3, "session_start_date": t(10), "vin": "A", "car_name": "Bravo", "where": "Home", "kwh": 30.0, "cost": 12.0, "duration": timedelta(hours=3)}]
    software = [{"vin": "B", "car_name": "Charlie", "version": "2026.26.6", "seen_at": t(12)}]
    days = timeline_days(drives, charges, software, PT)
    assert [d["date"].isoformat() for d in days] == ["2026-09-05", "2026-09-04"]
    assert [(e["kind"], e["vin"]) for e in days[0]["events"]] == [("drive", "B"), ("software", "B"), ("charge", "A")]
    assert days[0]["miles"] == 21.0 and days[0]["kwh"] == 30.0 and days[1]["events"][0]["title"] == "Home → Work"


def test_charging_summary_groups_by_place_end_level_and_supercharger():
    from stats import charging_summary

    mk = lambda where, kwh, cost, end, dc=False, home=False: {
        "session_start_date": T0,
        "where": where,
        "kwh": kwh,
        "cost": cost,
        "start_level": end - 30,
        "end_level": end,
        "gained": 30.0,
        "duration": timedelta(hours=1),
        "charging_type": "DC" if dc else "AC",
        "at_home": home,
    }
    s = charging_summary([mk("Home", 30, 12, 90, home=True), mk("Home", 20, 8, 80, home=True), mk("Gilroy Supercharger", 40, None, 78, dc=True), {"session_start_date": None}])
    assert [p["where"] for p in s["places"]] == ["Home", "Gilroy Supercharger"] and s["places"][0]["charges"] == 2 and s["places"][0]["kwh"] == 50
    assert [(e["level"], e["charges"]) for e in s["ends"]] == [(90, 1), (80, 2)]  # 78 rounds to 80
    assert s["superchargers"][0]["where"] == "Gilroy Supercharger" and s["total_cost"] == 20 and s["total_charges"] == 3


def test_the_time_of_use_split_is_kept_and_adds_up_to_the_meter():
    """session_cost has always worked out which hours the energy was drawn in, to price
    the charge, and then thrown the split away. It is the one number a time-of-use
    customer actually wants — how much of my charging lands off-peak — and no page could
    show it because nothing stored it."""
    powers = [(T0, T0 + timedelta(hours=1), 10.0)]
    s = {"id": 1, "session_start_date": T0, "session_end_date": T0 + timedelta(hours=1), "session_position_latitude": 1.0, "session_position_longitude": 1.0, "real_charged": 12.0}
    ((_i, kwh, cost, _p, _h, off, part, peak, *_),) = charge_facts([s], powers, PT, lambda lat, lon: True)
    assert round(off + part + peak, 6) == round(kwh, 6), "the split has to reconcile with the meter, not the power profile"
    assert round(off, 2) == 12.0, "T0 is the middle of the night, so all of it is off-peak"
    assert round(cost, 2) == 4.80


def test_a_charge_away_from_home_has_no_split_because_it_has_no_plan():
    s = {"id": 2, "session_start_date": T0, "session_end_date": T0 + timedelta(hours=1), "session_position_latitude": 9.0, "session_position_longitude": 9.0, "real_charged": 30.0}
    ((_i, kwh, cost, plan, home, off, part, peak, *_),) = charge_facts([s], [], PT, lambda lat, lon: False)
    assert kwh == 30.0 and cost is None and plan is None and home is False
    assert (off, part, peak) == (None, None, None), "your rates do not apply to somebody else's charger"


def test_a_rate_needs_enough_nights_behind_it():
    """A median of nine stretches is a number, not a measurement, and it would sit on a
    page beside two that are measurements."""
    from facts import PARKED_MIN_SAMPLES, published

    assert published(1.19, 231) == (1.19, 231)
    assert published(1.19, PARKED_MIN_SAMPLES) == (1.19, PARKED_MIN_SAMPLES)
    assert published(1.19, PARKED_MIN_SAMPLES - 1) == (None, PARKED_MIN_SAMPLES - 1)


def test_a_car_that_has_never_done_it_reports_nothing():
    """The Lucid has no Sentry history at all, so it gets no Sentry figure rather than a
    zero, which would read as "Sentry is free"."""
    from facts import published

    assert published(None, 0) == (None, 0)
    assert published(None, 500) == (None, 500)


def test_a_fast_charge_away_from_home_is_priced_at_the_dc_rate_when_one_is_set():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    t0 = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    s = {
        "id": 9,
        "session_start_date": t0,
        "session_end_date": t0 + timedelta(minutes=30),
        "session_position_latitude": 1.0,
        "session_position_longitude": 2.0,
        "real_charged": 40.0,
        "charging_type": "DC",
    }
    away = lambda lat, lon: False  # noqa: E731
    out = charge_facts([s], [], ZoneInfo("America/Los_Angeles"), away, dc_price=0.48)
    assert out[0][:4] == (9, 40.0, 19.2, "DC at 0.48/kWh") and out[0][4] is False
    assert charge_facts([s], [], ZoneInfo("America/Los_Angeles"), away)[0][2] is None, "no rate, no cost"
    assert charge_facts([{**s, "charging_type": "AC"}], [], ZoneInfo("America/Los_Angeles"), away, dc_price=0.48)[0][2] is None, "only fast charging"


def test_fast_charge_priced_by_the_minute_as_well():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from facts import charge_facts

    t0 = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    s = {
        "id": 9,
        "session_start_date": t0,
        "session_end_date": t0 + timedelta(minutes=30),
        "session_position_latitude": 2.0,
        "session_position_longitude": 2.0,
        "real_charged": 40.0,
        "charging_type": "DC",
        "real_cost": None,
    }
    away = lambda lat, lon: False  # noqa: E731
    (row,) = charge_facts([s], [], ZoneInfo("America/Los_Angeles"), away, dc_price=0.48, dc_per_min=0.10)
    assert row[2] == 40.0 * 0.48 + 30 * 0.10 and row[3] == "DC at 0.48/kWh + 0.10/min"
    (row,) = charge_facts([s], [], ZoneInfo("America/Los_Angeles"), away, dc_price=None, dc_per_min=0.10)
    assert row[2] == 3.0 and row[3] == "DC at 0.10/min", "by the minute alone"


def test_a_home_charge_with_no_plan_is_left_unpriced():
    """A new install has no plan. Its home charges carry no cost and no source, rather than
    a price on somebody else's utility; a fast charge away still takes the DC rate."""
    import cost

    cost.use_plan(None)
    home = {
        "id": 1,
        "session_start_date": T0,
        "session_end_date": T0 + timedelta(hours=1),
        "session_position_latitude": 1.0,
        "session_position_longitude": 1.0,
        "real_charged": 12.0,
        "charging_type": "AC",
    }
    (row,) = charge_facts([home], [], PT, lambda lat, lon: True, dc_price=0.48)
    assert row == (1, 12.0, None, None, True, None, None, None, None, "none", "", None)
    (row,) = charge_facts([dict(home, charging_type="DC")], [], PT, lambda lat, lon: True, dc_price=0.48)
    assert row[2] is None, "the fast-charging rate is for chargers away from home"
    (row,) = charge_facts([dict(home, real_cost=5.5)], [], PT, lambda lat, lon: True)
    assert row[2] == 5.5 and row[3] == "entered by hand" and row[9] == "hand"


def test_each_cost_says_where_it_came_from_and_keeps_the_exact_split():
    import json

    import cost

    s = {
        "id": 1,
        "session_start_date": T0,
        "session_end_date": T0 + timedelta(hours=1),
        "session_position_latitude": 1.0,
        "session_position_longitude": 1.0,
        "real_charged": 12.0,
        "charging_type": "DC",
    }
    (row,) = charge_facts([s], [(T0, T0 + timedelta(hours=1), 10.0)], PT, lambda lat, lon: True)
    assert row[9] == "plan" and json.loads(row[8]) == {"off_peak": 12.0, "part_peak": 0.0, "peak": 0.0}
    (row,) = charge_facts([s], [], PT, lambda lat, lon: False, dc_price=0.5)
    assert row[9] == "dc" and row[8] is None
    # Four periods: the three columns hold the split by role, the JSON the exact one.
    grid = [[0] * 24 for _ in range(12)]
    for m in range(12):
        grid[m][23], grid[m][0], grid[m][1] = 1, 2, 3  # T0 is 11pm local
    periods = [{"key": k, "label": k, "price": p} for k, p in (("a", 0.1), ("b", 0.2), ("c", 0.3), ("d", 0.4))]
    cost.use_plan({"name": "Springfield TOU", "source": "urdb:x", "versions": [{"from": "2020-01-01", "periods": periods, "weekday": grid, "weekend": grid}]})
    s = dict(s, session_end_date=T0 + timedelta(hours=3), real_charged=None)
    (row,) = charge_facts([s], [(T0, T0 + timedelta(hours=3), 10.0)], PT, lambda lat, lon: True)
    _i, kwh, price, plan, home, off, part, peak, exact, source, _region, _month = row
    assert json.loads(exact) == {"a": 0.0, "b": 10.0, "c": 10.0, "d": 10.0} and (off, part, peak) == (0.0, 20.0, 10.0)
    assert round(price, 6) == round(10 * 0.2 + 10 * 0.3 + 10 * 0.4, 6) and plan == "Springfield TOU" and source == "plan"


def test_the_split_by_period_prices_a_plan_with_more_than_three():
    """With the exact split stored, each period is costed at its own price; part-peak is
    then the sum of the periods it holds, not their energy at an averaged rate."""
    import cost
    from stats import tou_split

    grid = [[0] * 24 for _ in range(12)]
    periods = [{"key": k, "label": k, "price": p} for k, p in (("a", 0.1), ("b", 0.2), ("c", 0.5), ("d", 0.9))]
    cost.use_plan({"name": "Springfield TOU", "source": "urdb:x", "versions": [{"from": "2020-01-01", "periods": periods, "weekday": grid, "weekend": grid}]})
    c = {"at_home": True, "session_start_date": T0, "off_peak_kwh": 10.0, "part_peak_kwh": 11.0, "peak_kwh": 1.0, "period_kwh": {"a": 10.0, "b": 10.0, "c": 1.0, "d": 1.0}}
    by = {p["key"]: p for p in tou_split([c])["periods"]}
    assert round(by["part_peak"]["cost"], 6) == round(10 * 0.2 + 1 * 0.5, 6) and round(by["peak"]["cost"], 6) == 0.9 and round(by["off_peak"]["cost"], 6) == 1.0

import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import cost
from conftest import SPRINGFIELD_SCHEDULE
from cost import clean_schedule, compile_rows, fits_rows, grids_from_rows, period_at, plan_rows, rows_from_grids, schedule_text, session_cost, version_at

PT = ZoneInfo("America/Los_Angeles")


def _grid(fill: int = 0) -> list[list[int]]:
    return [[fill] * 24 for _ in range(12)]


# A four-period plan in the shape an imported tariff takes: periods keyed by number, a
# super off-peak in the sunny middle of the day, summer and winter hours, and weekends
# without a peak. Two versions, and no reaching back before the first.
def _four_period_plan() -> dict:
    weekday, weekend = _grid(1), _grid(1)  # off-peak by default
    for m in range(12):
        summer = 5 <= m <= 8  # June-September
        for h in range(9, 14):
            weekday[m][h] = weekend[m][h] = 0  # super off-peak 9am-2pm
        for h in range(16, 21):
            weekday[m][h] = 3 if summer else 2  # peak in summer, mid-peak in winter
            weekend[m][h] = 2  # weekends top out at mid-peak
    periods = lambda a, b, c, d: [  # noqa: E731
        {"key": "p0", "label": "Super off-peak", "price": a},
        {"key": "p1", "label": "Off-peak", "price": b},
        {"key": "p2", "label": "Mid-peak", "price": c},
        {"key": "p3", "label": "Peak", "price": d},
    ]
    return {
        "name": "Springfield Solar TOU",
        "utility": "Springfield Power",
        "source": "urdb:test",
        "versions": [
            {"from": "2024-01-01", "periods": periods(0.20, 0.30, 0.40, 0.55), "weekday": weekday, "weekend": weekend},
            {"from": "2026-01-01", "periods": periods(0.25, 0.35, 0.45, 0.60), "weekday": weekday, "weekend": weekend, "fixed_note": "Plus $12 a month fixed."},
        ],
    }


@pytest.fixture
def no_plan():
    cost.use_plan(None)
    yield
    cost.use_plan(None)


def test_schedule_rows_pick_the_period_by_day_hour_and_month():
    mon = datetime(2026, 9, 28, 16)  # a Monday in September
    sat = datetime(2026, 10, 3, 16)  # a Saturday in October
    s = SPRINGFIELD_SCHEDULE
    assert period_at(mon, s) == "peak" and period_at(mon.replace(hour=15), s) == "part_peak" and period_at(mon.replace(hour=21), s) == "off_peak", "9pm is part-peak only outside June-September"
    assert period_at(sat.replace(hour=21), s) == "part_peak" and period_at(sat.replace(hour=3), s) == "off_peak"
    weekday_only = [{"period": "peak", "days": "weekdays", "start": 17, "end": 20, "months": None}]
    assert period_at(mon.replace(hour=18), weekday_only) == "peak" and period_at(sat.replace(hour=18), weekday_only) == "off_peak"
    assert period_at(mon.replace(hour=18), []) == "off_peak", "an empty schedule is flat"
    rows = clean_schedule(
        [
            {"period": "peak", "days": "weekends", "start": "17", "end": "20", "months": ["6", "7"]},
            {"period": "nope", "start": 1, "end": 2},
            {"period": "peak", "days": "all", "start": 20, "end": 20},
            {"period": "peak", "days": "all", "start": "", "end": ""},
        ]
    )
    assert rows == [{"period": "peak", "days": "weekends", "start": 17, "end": 20, "months": [6, 7]}], "a bad period, an empty span and blank hours are dropped"
    assert clean_schedule(None) == []
    assert clean_schedule([{"period": "peak", "days": "all", "start": 1, "end": 2, "months": list(range(1, 13))}])[0]["months"] is None, "all twelve ticked is the whole year"
    text = schedule_text(s)
    assert text.startswith("peak 4pm–9pm; part-peak 3pm–4pm; part-peak 9pm–10pm in Jan, Feb") and text.endswith("; off-peak otherwise.")
    assert schedule_text([]) == "off-peak all day."


def test_rows_compile_to_a_grid_and_back():
    weekday, weekend = grids_from_rows(SPRINGFIELD_SCHEDULE)
    assert weekday == weekend, "every row applies every day"
    assert weekday[6][15] == 1 and weekday[6][16] == 2 and weekday[6][20] == 2 and weekday[6][21] == 0, "July: part-peak 3pm, peak 4-9pm, off-peak at 9pm"
    assert weekday[0][21] == 1 and weekday[0][22] == 0, "January: part-peak 9-10pm"
    assert rows_from_grids(weekday, weekend) == SPRINGFIELD_SCHEDULE, "the editor's rows come back as they were typed"
    mixed = [
        {"period": "peak", "days": "weekdays", "start": 17, "end": 20, "months": [6, 7, 8]},
        {"period": "part_peak", "days": "weekends", "start": 17, "end": 20, "months": None},
        {"period": "part_peak", "days": "all", "start": 7, "end": 9, "months": [12]},
    ]
    wd, we = grids_from_rows(mixed)
    assert wd[6][18] == 2 and we[6][18] == 1 and wd[0][18] == 0 and we[0][18] == 1 and wd[11][8] == we[11][8] == 1
    back = rows_from_grids(wd, we)
    assert grids_from_rows(back) == (wd, we)
    assert {"period": "part_peak", "days": "all", "start": 7, "end": 9, "months": [12]} in back and {"period": "peak", "days": "weekdays", "start": 17, "end": 20, "months": [6, 7, 8]} in back


def test_any_three_period_grid_survives_rows_and_back():
    """The rows are a second spelling of the grid, so any grid of the three periods has to
    come back cell for cell, overlapping first-match rows and all."""
    rng = random.Random(11)
    for _ in range(40):
        wd = [[rng.choice((0, 0, 0, 1, 2)) for _h in range(24)] for _m in range(12)]
        we = [[rng.choice((0, 0, 1, 2)) for _h in range(24)] for _m in range(12)]
        assert grids_from_rows(rows_from_grids(wd, we)) == (wd, we)


def test_a_manual_plan_fits_the_rows_editor_and_others_do_not():
    rates = [{"from": "2025-01-01", "off_peak": 0.3, "part_peak": 0.4, "peak": 0.5}, {"from": "2020-01-01", "off_peak": 0.2, "part_peak": 0.3, "peak": 0.4}]
    plan = compile_rows("Springfield EV", rates, SPRINGFIELD_SCHEDULE)
    assert [v["from"] for v in plan["versions"]] == ["2020-01-01", "2025-01-01"] and plan["source"] == "manual" and cost.valid_plan(plan)
    assert fits_rows(plan) and plan_rows(plan) == (sorted(rates, key=lambda r: r["from"]), SPRINGFIELD_SCHEDULE)
    four = _four_period_plan()
    assert cost.valid_plan(four) and not fits_rows(four) and plan_rows(four) == ([], [])
    assert not fits_rows(None) and plan_rows(None) == ([], [])
    assert not cost.valid_plan({"versions": [{"from": "2026-01-01", "periods": [{"key": "a", "price": 0.1}], "weekday": _grid(1), "weekend": _grid(0)}]}), "a cell naming a period that is not there"


def test_four_periods_price_by_their_own_hours_and_fold_into_roles(no_plan):
    plan = _four_period_plan()
    cost.use_plan(plan)
    assert cost.current_plan() == "Springfield Solar TOU"
    v = version_at(date(2026, 7, 1))
    assert cost.roles(v) == ["off_peak", "part_peak", "part_peak", "peak"], "cheapest is off-peak, dearest peak, the two between part-peak"
    # A July weekday, 8am to 6pm at 10 kW: 1 h off-peak, 5 h super off-peak, 2 h off-peak, 2 h peak.
    start = datetime(2026, 7, 1, 8, tzinfo=PT)
    end = datetime(2026, 7, 1, 18, tzinfo=PT)
    c = session_cost([(start, end, 10.0)], start, end, PT)
    assert c["kwh"] == pytest.approx({"p0": 50.0, "p1": 30.0, "p2": 0.0, "p3": 20.0})
    assert c["cost"] == pytest.approx(50 * 0.25 + 30 * 0.35 + 20 * 0.60)
    assert c["roles"] == pytest.approx({"off_peak": 50.0, "part_peak": 30.0, "peak": 20.0})
    assert c["labels"]["p0"] == "Super off-peak" and c["plan"] == "Springfield Solar TOU"
    # The same hours in January are mid-peak, and the older version's prices apply in 2025.
    jan = session_cost([(start.replace(month=1, year=2025), end.replace(month=1, year=2025), 10.0)], start.replace(month=1, year=2025), end.replace(month=1, year=2025), PT)
    assert jan["kwh"]["p3"] == 0.0 and jan["kwh"]["p2"] == pytest.approx(20.0) and jan["cost"] == pytest.approx(50 * 0.20 + 30 * 0.30 + 20 * 0.40)
    rates = cost.rates_at(date(2026, 7, 1))
    assert rates == pytest.approx({"off_peak": 0.25, "part_peak": 0.40, "peak": 0.60}), "part-peak is the mean of the periods between"
    assert cost.role_costs(v, c["kwh"]) == pytest.approx({"off_peak": 12.5, "part_peak": 10.5, "peak": 12.0})


def test_no_plan_or_a_date_before_the_plan_prices_nothing(no_plan):
    start = datetime(2026, 7, 1, 8, tzinfo=PT)
    end = start + timedelta(hours=2)
    assert session_cost([(start, end, 10.0)], start, end, PT) is None and cost.current_plan() is None and cost.rates_at(start) == {}
    cost.use_plan(_four_period_plan())
    old = datetime(2023, 7, 1, 8, tzinfo=PT)
    assert session_cost([(old, old + timedelta(hours=1), 10.0)], old, old + timedelta(hours=1), PT) is None, "an imported plan covers only its own dates"
    cost.use_plan(compile_rows("Home", [{"from": "2025-01-01", "off_peak": 0.3, "part_peak": 0.4, "peak": 0.5}], []))
    priced = session_cost([(old, old + timedelta(hours=1), 10.0)], old, old + timedelta(hours=1), PT)
    assert priced["cost"] == pytest.approx(3.0), "a typed-in plan's first row stands for everything before it"
    cost.use_plan({"name": "broken", "versions": [{"from": "nope"}]})
    assert cost.current_plan() is None, "a plan that does not parse prices nothing rather than failing every page"


def test_daylight_saving_days_price_each_real_hour(no_plan):
    """Spring forward: 2am does not happen, so 1am to 4am is two real hours. Fall back: 1am
    happens twice, and both hours are priced as 1am."""
    weekday, weekend = _grid(0), _grid(0)
    for g in (weekday, weekend):
        for m in range(12):
            g[m][1] = 1
            g[m][3] = 2
    plan = {
        "name": "Springfield",
        "source": "manual",
        "versions": [{"from": "2000-01-01", "periods": [{"key": "a", "price": 0.1}, {"key": "b", "price": 0.2}, {"key": "c", "price": 0.3}], "weekday": weekday, "weekend": weekend}],
    }
    start = datetime(2026, 3, 8, 1, 0, tzinfo=PT)
    end = datetime(2026, 3, 8, 4, 0, tzinfo=PT)
    assert (end - start) == timedelta(hours=3) and (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)) == timedelta(hours=2)
    s, e = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    c = session_cost([(s, e, 6.0)], s, e, PT, plan)
    assert c["kwh"] == pytest.approx({"a": 0.0, "b": 6.0, "c": 6.0}), "1am then 3am, nothing at a 2am that never came"
    s = datetime(2026, 11, 1, 0, 30, tzinfo=PT).astimezone(timezone.utc)
    e = s + timedelta(hours=3)  # 12:30am PDT to 2:30am PST
    c = session_cost([(s, e, 6.0)], s, e, PT, plan)
    assert c["kwh"] == pytest.approx({"a": 6.0, "b": 12.0, "c": 0.0}), "half an hour at midnight, two hours of 1am, half an hour at 2am"


def test_a_charge_across_a_month_edge_and_into_the_weekend_changes_grid():
    weekday, weekend = _grid(0), _grid(0)
    weekday[1] = [1] * 24  # February weekdays: all period 1
    weekend[1] = [2] * 24  # February weekends: all period 2
    plan = {
        "name": "Springfield",
        "source": "manual",
        "versions": [{"from": "2000-01-01", "periods": [{"key": "a", "price": 0.1}, {"key": "b", "price": 0.2}, {"key": "c", "price": 0.3}], "weekday": weekday, "weekend": weekend}],
    }
    s = datetime(2026, 1, 31, 22, tzinfo=PT)
    e = datetime(2026, 2, 1, 2, tzinfo=PT)
    assert s.weekday() == 5 and e.weekday() == 6
    c = session_cost([(s, e, 5.0)], s, e, PT, plan)
    assert c["kwh"] == pytest.approx({"a": 10.0, "b": 0.0, "c": 10.0}), "two January hours, then two February weekend hours"
    s = datetime(2026, 2, 27, 22, tzinfo=PT)  # Friday into Saturday, within February
    e = datetime(2026, 2, 28, 2, tzinfo=PT)
    c = session_cost([(s, e, 5.0)], s, e, PT, plan)
    assert c["kwh"] == pytest.approx({"a": 0.0, "b": 10.0, "c": 10.0}), "Friday's hours on the weekday grid, Saturday's on the weekend one"


# The pricing as it was before plans: rate rows by date (the first standing for anything
# earlier), first-match schedule rows, unmatched hours off-peak. Kept here only as the
# reference the migration has to reproduce exactly.
def _old_price(powers, start, end, tz, history, schedule):
    def period(local):
        weekend = local.weekday() >= 5
        for r in schedule:
            if r["days"] == "weekdays" and weekend or r["days"] == "weekends" and not weekend:
                continue
            if r["months"] and local.month not in r["months"]:
                continue
            if r["start"] <= local.hour < r["end"]:
                return r["period"]
        return "off_peak"

    kwh = {"off_peak": 0.0, "part_peak": 0.0, "peak": 0.0}
    for a, b, kw in powers:
        if kw is None or kw <= 0:
            continue
        t, stop = max(a, start), min(b, end)
        while t < stop:
            local = t.astimezone(tz)
            next_hour = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(t.tzinfo)
            seg_end = min(stop, next_hour)
            kwh[period(local)] += kw * (seg_end - t).total_seconds() / 3600
            t = seg_end
    d = start.astimezone(tz).date()
    rates = history[0][1]
    for s, table in history:
        if s <= d:
            rates = table
    return kwh, sum(kwh[p] * rates[p] for p in kwh)


def _fixture_sessions(n: int = 400):
    """Charges from 2019 to 2027 at all hours, a few power steps each, some across midnight,
    month ends and the clock changes."""
    rng = random.Random(7)
    out = []
    base = datetime(2019, 1, 1, tzinfo=timezone.utc)
    for _ in range(n):
        start = base + timedelta(minutes=rng.randrange(0, 8 * 365 * 24 * 60))
        t, powers = start, []
        for _step in range(rng.randint(1, 5)):
            dur = timedelta(minutes=rng.randint(5, 240))
            powers.append((t, t + dur, rng.choice((1.4, 7.2, 11.0, 0.0, 9.6))))
            t += dur
        out.append((powers, start, t))
    for when in (datetime(2026, 3, 8, 0, 30, tzinfo=PT), datetime(2026, 11, 1, 0, 30, tzinfo=PT), datetime(2026, 1, 31, 22, tzinfo=PT), datetime(2025, 12, 31, 23, tzinfo=PT)):
        s = when.astimezone(timezone.utc)
        out.append(([(s, s + timedelta(hours=4), 7.2)], s, s + timedelta(hours=4)))
    return out


@pytest.mark.parametrize(
    "stored",
    [
        {},  # never saved Money: the built-in rates and schedule
        {
            "plan": "Springfield EV",
            "rates": [
                {"from": "2026-01-01", "off_peak": 0.41, "part_peak": 0.58, "peak": 0.66},
                {"from": "2021-06-01", "off_peak": 0.22, "part_peak": 0.35, "peak": 0.48},
                {"from": "2023-03-15", "off_peak": 0.31, "part_peak": 0.44, "peak": 0.59},
            ],
            "schedule": [  # overlapping rows: the first match has to keep winning
                {"period": "peak", "days": "weekdays", "start": 16, "end": 21, "months": [6, 7, 8, 9]},
                {"period": "part_peak", "days": "all", "start": 14, "end": 22, "months": None},
                {"period": "peak", "days": "weekends", "start": 17, "end": 19, "months": [12, 1]},
                {"period": "part_peak", "days": "weekends", "start": 7, "end": 10, "months": None},
            ],
        },
    ],
)
def test_upgrading_to_plans_changes_no_cost(stored, no_plan):
    """Every charge priced before and after the move to plans: same cost, same split, same total."""
    import settings as settings_mod

    rows = sorted(stored.get("rates") or cost.LEGACY_RATES, key=lambda r: r["from"])
    history = [(date.fromisoformat(r["from"]), {k: r[k] for k in cost.ROLES}) for r in rows]
    schedule = clean_schedule(stored.get("schedule")) or cost.LEGACY_SCHEDULE

    class Store:
        def __init__(self, rows):
            self.rows = {"units": "imperial", **rows}

        def execute(self, sql, args=()):
            import json

            if sql.lstrip().startswith("INSERT"):
                self.rows[args[0]] = json.loads(args[1])
            data = [{"key": k, "value": v} for k, v in self.rows.items()]

            class R:
                def fetchall(self_inner):
                    return data

            return R()

    plan = settings_mod.load(Store(stored))["rate_plan"]
    cost.use_plan(plan)
    before = after = 0.0
    for powers, start, end in _fixture_sessions():
        old_kwh, old_cost = _old_price(powers, start, end, PT, history, schedule)
        new = session_cost(powers, start, end, PT)
        assert new["cost"] == pytest.approx(old_cost, abs=1e-12) and new["roles"] == pytest.approx(old_kwh, abs=1e-12), start
        before += old_cost
        after += new["cost"]
    assert after == pytest.approx(before, abs=1e-9) and before > 0


def test_a_profile_of_charging_prices_like_the_charges_themselves(no_plan):
    """Charges folded into weekday/weekend x month x hour, priced on a plan's grid, cost what
    each charge priced alone costs at the same version: the profile loses only the date."""
    plan = _four_period_plan()
    profile = [0.0] * 576
    charges = [
        (datetime(2026, 7, 1, 8, tzinfo=PT), datetime(2026, 7, 1, 18, tzinfo=PT), 10.0),  # a July weekday
        (datetime(2026, 7, 4, 15, 30, tzinfo=PT), datetime(2026, 7, 4, 22, tzinfo=PT), 7.2),  # a July Saturday
        (datetime(2026, 1, 13, 23, tzinfo=PT), datetime(2026, 1, 14, 6, tzinfo=PT), 11.0),  # over midnight in January
    ]
    for a, b, kw in charges:
        cost.profile_add(profile, [(a, b, kw)], a, b, PT)
    assert sum(profile) == pytest.approx(10 * 10 + 6.5 * 7.2 + 7 * 11)
    assert profile[cost.profile_index(True, 7, 16)] == pytest.approx(7.2) and profile[cost.profile_index(False, 7, 16)] == pytest.approx(10.0)
    alone = sum(session_cost([(a, b, kw)], a, b, PT, plan)["cost"] for a, b, kw in charges)
    assert cost.profile_cost(plan, profile, date(2026, 10, 1)) == pytest.approx(alone)
    # at the prices in force on another day: the older version; before the first, the first
    older = cost.with_prices(plan, {})["versions"][0]
    assert cost.profile_cost(plan, profile, date(2025, 6, 1)) == cost.profile_cost({**plan, "versions": [older]}, profile, date(2025, 6, 1))
    assert cost.profile_cost(plan, profile, date(2020, 1, 1)) == cost.profile_cost(plan, profile, date(2025, 6, 1))
    assert cost.profile_cost(None, profile, date(2026, 1, 1)) is None


def seasonal(part_peak_late):
    """A plan as OpenEI files one: summer and winter periods side by side, six in all."""
    def row(off, part, peak):
        return [off] * 15 + [part] + [peak] * 5 + [part] * 3 if part_peak_late else [off] * 15 + [part] + [peak] * 5 + [off] * 3

    summer, winter = row(0, 1, 2), row(3, 4, 5)
    grid = [winter if m < 5 or m > 8 else summer for m in range(12)]
    prices = [0.22, 0.43, 0.54, 0.22, 0.39, 0.41]
    return {"periods": [{"key": str(i), "label": str(i), "price": p} for i, p in enumerate(prices)], "weekday": grid, "weekend": grid}


def test_hours_in_a_different_role_are_found_and_said_by_month():
    late = [{"period": "peak", "days": "all", "start": 16, "end": 21, "months": None}, {"period": "part_peak", "days": "all", "start": 15, "end": 16, "months": None}]
    mine = compile_rows("Springfield EV", [{"from": "2026-01-01", "off_peak": 0.3, "part_peak": 0.49, "peak": 0.52}], late + [{"period": "part_peak", "days": "all", "start": 21, "end": 22, "months": [1, 2, 3, 4, 5, 10, 11, 12]}])["versions"][0]
    assert cost.hours_differ(mine, seasonal(True)) == ["off-peak here, part-peak there: 10pm–12am Jan–May, Oct–Dec; 9pm–12am Jun–Sep"]
    right = compile_rows("Springfield EV", [{"from": "2026-01-01", "off_peak": 0.3, "part_peak": 0.49, "peak": 0.52}], late + [{"period": "part_peak", "days": "all", "start": 21, "end": 24, "months": None}])["versions"][0]
    assert cost.hours_differ(right, seasonal(True)) == []  # six seasonal periods rank within each month, so winter's peak is still peak
    assert cost.hours_differ(right, seasonal(False)) == ["part-peak here, off-peak there: 9pm–12am every month"]

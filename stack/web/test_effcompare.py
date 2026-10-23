"""Efficiency compare-with: the second set by period, tire set or tag, paired with the
first bin by bin on one scale, and the change read the same way everywhere."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import effcompare

TZ = ZoneInfo("America/Chicago")


def d(day, tag=None, miles=10.0, kwh=3.0):
    return {"start_date": datetime(2026, 1, day, 18, 0, tzinfo=timezone.utc), "tag": tag, "miles": miles, "kwh_used": kwh}


def test_the_choices_are_periods_then_tire_sets_then_tags():
    keys = [c["key"] for c in effcompare.choices(["Winter"], ["Commute"], True)]
    assert keys == ["", "prev", "yoy", "tires:Winter", "tag:Commute"]
    assert [c["key"] for c in effcompare.choices([], [], False)] == [""], "all time has no period before it"


def test_a_tire_set_splits_the_period_by_the_days_it_was_on_removed_day_included():
    drives = [d(1), d(5), d(10), d(20)]
    windows = [(date(2026, 1, 5), date(2026, 1, 10))]
    rest, on = effcompare.split(drives, "tires:Winter", windows, TZ)
    assert [x["start_date"].day for x in on] == [5, 10] and [x["start_date"].day for x in rest] == [1, 20]
    rest, on = effcompare.split(drives, "tires:Winter", [(date(2026, 1, 19), None)], TZ)
    assert [x["start_date"].day for x in on] == [20], "a set still on runs to today"


def test_a_tag_is_matched_without_regard_to_case():
    rest, on = effcompare.split([d(1, "Commute"), d(2, "commute "), d(3)], "tag:Commute", [], TZ)
    assert len(on) == 2 and len(rest) == 1


def row(lo, wh, drives=12, miles=100.0):
    return {"lo": lo, "hi": lo + 10, "wh_per_mi": wh, "drives": drives, "miles": miles}


def test_pairs_share_one_scale_from_zero_and_say_the_difference():
    a = [row(40, 300.0), row(50, 260.0)]
    b = [row(40, 330.0), row(60, 250.0, drives=4)]
    rows = effcompare.pair(a, b)
    assert [r["lo"] for r in rows] == [40, 50, 60]
    first = rows[0]
    assert first["diff"] == 30.0 and round(first["change"], 3) == 0.1 and first["share_b"] == 1.0 and round(first["share_a"], 3) == 0.909, "from zero: 300 is 91 % of 330"
    assert first["strong"] and not first["few"], "a tenth more on a dozen drives a side is worth saying"
    assert rows[1]["b"] is None and rows[1]["diff"] is None and rows[1]["few"] and rows[2]["a"] is None
    weak = effcompare.pair([row(40, 300.0)], [row(40, 305.0)])[0]
    assert not weak["strong"], "under three percent is not called"
    assert effcompare.pair([row(40, 300.0)], [row(40, 330.0, drives=6)])[0]["few"], "six drives on one side are few"


def test_the_headline_holds_temperature_level():
    """Winter tires driven mostly in the cold: pooled they look 25 % worse; band by band,
    weighted by where the winter tires drove, they are 5 % worse."""
    rows = effcompare.pair([row(30, 400.0, miles=50.0), row(70, 250.0, miles=500.0)], [row(30, 420.0, miles=400.0), row(70, 262.5, miles=10.0)])
    m = effcompare.matched(rows)
    assert round(m["change"], 3) == 0.05 and m["rows"] == 2
    assert effcompare.matched(effcompare.pair([row(30, 400.0)], [row(70, 300.0)])) is None, "no band both drove in"


def test_speed_is_read_at_the_same_temperatures():
    from datetime import timedelta

    from stats import SPEED_BINS, TEMP_BINS

    def dr(temp, kwh):
        return {"miles": 10.0, "kwh_used": kwh, "temp_f": temp, "duration": timedelta(minutes=20)}  # 30 mph

    rest = [dr(75, 2.5)] * 6 + [dr(35, 4.0)] * 2
    winter = [dr(35, 4.2)] * 6 + [dr(-40, 1.0)] * 3
    (r,) = effcompare.speed_matched(rest, winter, 1.0, TEMP_BINS, SPEED_BINS)
    assert (r["lo"], round(r["a"]["wh_per_mi"]), round(r["b"]["wh_per_mi"])) == (25, 400, 420), "only the cold band both drove in; -40 is no reading"
    assert r["a"]["drives"] == 2 and r["b"]["drives"] == 6


def test_overall_weighs_by_distance_and_skips_short_drives():
    o = effcompare.overall([d(1, miles=10, kwh=3.0), d(2, miles=30, kwh=6.0), d(3, miles=0.5, kwh=1.0)], 1.0)
    assert o["drives"] == 2 and round(o["wh_per_mi"]) == 225
    assert effcompare.overall([], 1.0) is None

"""The battery's lifetime report: points from the charge facts in buckets of miles, a trend
through them, loss three ways, and marks placed on the odometer without a query."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import lifetime
from units import Units

TZ = ZoneInfo("America/Chicago")
T0 = datetime(2023, 9, 1, tzinfo=timezone.utc)


def rows(n=300, fade=0.001, start=310.0, every=4, miles=45.0):
    """A charge every `every` days, `miles` apart, the range fading `fade` a mile, ending at 80 %."""
    return [(T0 + timedelta(days=i * every), 50 + i * miles, start - fade * i * miles + (1.5 if i % 2 else -1.5), 80.0) for i in range(n)]


def test_charges_that_stop_low_or_read_nonsense_are_not_points():
    rs = rows(20) + [(T0, 100.0, 300.0, 30.0), (T0, 120.0, 30.0, 90.0), (T0, 130.0, 900.0, 90.0), (T0, None, 300.0, 90.0)]
    assert len(lifetime.clean(rs)) == 20


def test_buckets_are_round_and_never_too_many():
    assert lifetime.bucket_size(14_000) == 200 and lifetime.bucket_size(95_000) == 1000 and lifetime.bucket_size(500) == 100
    rep = lifetime.report(rows(300))
    assert len(rep["points"]) <= lifetime.MAX_POINTS and sum(b["n"] for b in rep["points"]) == 300 == rep["charges"]


def test_the_trend_follows_the_fade_through_the_wobble():
    rep = lifetime.report(rows(300))
    first, last = rep["points"][0], rep["points"][-1]
    assert abs(first["trend"] - (310 - 0.001 * first["odo"] + 0.05)) < 1.0 and abs(last["trend"] - (310 - 0.001 * last["odo"])) < 1.0


def test_loss_is_read_three_ways_off_the_trend():
    rep = lifetime.report(rows(300), start=320.0)
    now = rep["now"]
    assert now == rep["points"][-1]["trend"] and abs(now - (310 - 0.001 * (50 + 45 * 299))) < 1.0, "now is the trend's end, not ten noisy charges"
    assert abs(rep["now_raw"] - (310 - 0.001 * 50 - 0.001 * 45 * 294.5)) < 0.6, "the median of the last ten stays for the tooltip"
    assert rep["losses"]["first"]["from"] == rep["first"] == rep["points"][0]["trend"] and rep["losses"]["first"]["to"] == now
    assert rep["best"]["full"] == max(b["trend"] for b in rep["points"]) and rep["best_raw"]["n"] >= 3
    start = rep["losses"]["start"]
    assert start["from"] == 320.0 and round(start["pct"], 4) == round((320 - now) / 320, 4)
    assert lifetime.report(rows(300))["losses"]["start"] is None
    assert lifetime.report(rows(2)) is None, "two charges are not a report"


def test_marks_land_on_the_odometer_by_time_without_a_query():
    rep = lifetime.report(rows(300))
    payload = lifetime.chart(rep, Units(), TZ, "odo")
    years = [m[1] for m in payload["marks"]]
    assert years == ["2024", "2025", "2026"] and payload["xkind"] == "dist" and payload["on"] == ["trend"]
    jan24 = next(m[0] for m in payload["marks"] if m[1] == "2024")
    assert 50 + 45 * 30 < jan24 < 50 + 45 * 32, "about 30 charges of 45 miles in, on the first of January"
    sw = lifetime.chart(rep, Units(), TZ, "date", [("2024.2.1", datetime(2024, 2, 1, tzinfo=timezone.utc)), ("2099.1", datetime(2099, 1, 1, tzinfo=timezone.utc))])
    assert [m[1] for m in sw["rug"]] == ["2024.2.1"] and "xkind" not in sw, "updates are ticks on the top edge; one outside the record is not drawn"
    assert [m[1] for m in sw["marks"]] == ["2024", "2025", "2026"], "the years stay marked"
    assert payload["primary"]["names"][0] == "Sep 6, 2023 · 118 mi" and payload["ticks"][0][1].endswith(" mi"), "the card names a point by date and odometer (the bucket's medians)"
    keys = [x["key"] for x in payload["extras"]]
    assert keys == ["trend", "n"] and payload["extras"][1]["label"] == "Charges", "how many charges each point rests on, in the hover card"


def test_metric_draws_kilometres_on_both_axes():
    rep = lifetime.report(rows(50))
    p = lifetime.chart(rep, Units(metric=True), TZ)
    assert p["xunit"] == " km" and p["primary"]["points"][0][1] > 450


def test_a_new_car_with_one_stretch_of_charges_still_has_a_date_axis():
    rs = [(T0 + timedelta(hours=h), 50.0 + h, 310.0, 80.0) for h in range(3)]
    p = lifetime.chart(lifetime.report(rs), Units(), TZ, "date")
    assert p["t1"] > p["t0"], "a single bucket is one moment; the axis gets a day"


def test_the_axis_labels_land_on_round_figures():
    assert lifetime.round_axis([293.4, 311.0]) == (280, 320)
    assert lifetime.round_axis([291.0, 309.5]) == (290, 310), "290 / 300 / 310"
    assert lifetime.round_axis([300.0, 300.4]) == (300, 302), "a step under one would print 300 three times"
    lo, hi = lifetime.round_axis([447.0, 489.0])
    assert ((lo + hi) / 2) % 10 == 0 and lo <= 447 and hi >= 489

"""Bar strips: a bar a day, month or week, placed on one scale, from totals the page already has."""

from datetime import date

from strips import H, W, month_days, strip, year_months, year_weeks


def test_a_strip_scales_to_its_tallest_bar_and_keeps_empty_days_visible():
    s = strip([{"value": 10, "label": "a"}, {"value": 5, "label": "b"}, {"value": None, "label": "c"}, {"value": 3, "label": "d", "future": True}])
    a, b, c, d = s["bars"]
    assert a["y"] == H - a["h"] and a["h"] == H - 1 and b["h"] == (H - 1) / 2
    assert c["h"] == 1 and d["h"] == 0, "a day with nothing is a sliver, a future one is empty"
    assert a["x"] < b["x"] < c["x"] and d["x"] + d["w"] <= W and s["total"] == 18
    assert strip([])["bars"] == [] and strip([{"value": 0, "label": "z"}])["bars"][0]["h"] == 1


def test_a_month_is_a_bar_a_day_with_today_marked_and_links():
    vals = {date(2026, 10, 1): 12.0, date(2026, 10, 7): 42.0}
    s = month_days(2026, 10, vals, today=date(2026, 10, 13), fmt=lambda v: f"{v:.0f} mi", href=lambda d: f"/day?date={d}")
    bars = s["bars"]
    assert len(bars) == 31 and bars[6]["label"] == "Wed Oct 7: 42 mi" and bars[1]["label"] == "Fri Oct 2: nothing"
    assert [i for i, b in enumerate(bars) if b["current"]] == [12] and bars[13]["future"] and not bars[12]["future"]
    assert bars[0]["href"] == "/day?date=2026-10-01" and [t["text"] for t in s["ticks"]] == ["1", "8", "15", "22", "29"]
    assert len(month_days(2026, 2, {})["bars"]) == 28 and not any(b["href"] for b in month_days(2026, 2, {})["bars"])


def test_a_year_is_twelve_months_keyed_either_way():
    s = year_months(2026, {1: 900.0, "2026-03": 1200.0}, today=date(2026, 10, 13))
    bars = s["bars"]
    assert len(bars) == 12 and bars[0]["value"] == 900.0 and bars[2]["value"] == 1200.0 and bars[2]["label"] == "March 2026: 1,200"
    assert bars[9]["current"] and bars[10]["future"] and not bars[8]["future"]
    assert [t["text"] for t in s["ticks"]] == list("JFMAMJJASOND")


def test_weeks_sum_their_days_inside_the_year_only():
    daily = {date(2025, 12, 31): 99.0, date(2026, 1, 1): 10.0, date(2026, 1, 4): 5.0, date(2026, 1, 5): 7.0, date(2026, 12, 31): 3.0}
    s = year_weeks(2026, daily, today=date(2026, 1, 6))
    bars = s["bars"]
    # 2026 starts on a Thursday: the first Monday week is Dec 29–Jan 4, holding Jan 1–4 only
    assert len(bars) == 53 and bars[0]["value"] == 15.0 and bars[0]["label"].startswith("Week of Jan 1:")
    assert bars[1]["value"] == 7.0 and bars[1]["current"] and bars[2]["future"] and bars[-1]["value"] == 3.0
    assert [t["text"] for t in s["ticks"]] == ["Jan", "Apr", "Jul", "Oct"]
    sun = year_weeks(2026, daily, week_start="sunday")["bars"]
    assert sun[0]["value"] == 10.0 and sun[1]["value"] == 12.0, "Sunday weeks: Jan 1–3, then Jan 4–10"


def test_the_strip_macro_draws_titles_links_and_the_current_bar():
    from test_parts import render

    s = month_days(2026, 10, {date(2026, 10, 7): 42.0}, today=date(2026, 10, 13), href=lambda d: f"/day?date={d}")
    html = render('{% import "_strips.html" as st %}{{ st.strip(s, "Distance each day of October") }}', s=s)
    assert html.count("<title>") == 31 and "<title>Wed Oct 7: 42</title>" in html and 'href="/day?date=2026-10-07"' in html
    assert html.count('class="sb cur nil"') == 1 and 'aria-label="Distance each day of October"' in html and 'preserveAspectRatio="none"' in html
    assert 'style="left:' in html and ">29<" in html
    plain = render('{% import "_strips.html" as st %}{{ st.strip(s, "x") }}', s=year_months(2026, {}))
    assert "<a " not in plain


def test_weeks_take_the_calendar_setting_as_a_number_too():
    from strips import year_weeks

    labels = lambda ws: [b["label"] for b in year_weeks(2026, {}, week_start=ws)["bars"]]  # noqa: E731
    assert labels(6) == labels("sunday") != labels("monday")

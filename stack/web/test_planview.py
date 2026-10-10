"""Settings › Money's view of the home plan."""

import cost
from conftest import SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE
from datetime import date

from planview import grid_view, money_context, months_text, price_text, prices_summary


def _four(weekend_same: bool = True) -> dict:
    weekday = [[1] * 9 + [0] * 5 + [1] * 2 + [3] * 5 + [2] * 3 for _ in range(12)]
    weekend = weekday if weekend_same else [[1] * 24 for _ in range(12)]
    periods = lambda a: [{"key": f"p{i}", "label": name, "price": a + i * 0.1} for i, name in enumerate(("Super off-peak", "Off-peak", "Mid-peak", "Peak"))]  # noqa: E731
    return {
        "name": "Springfield Solar TOU",
        "source": "urdb:test",
        "versions": [
            {"from": "2024-01-01", "periods": periods(0.1), "weekday": weekday, "weekend": weekend},
            {"from": "2026-01-01", "periods": periods(0.2), "weekday": weekday, "weekend": weekend, "fixed_note": "Plus $12 a month fixed."},
        ],
    }


def test_no_plan_and_a_manual_plan_use_the_rows_editor():
    ctx = money_context(None)
    assert ctx["plan"] is None and ctx["plan_fits"] and ctx["plan_rates"] == [] and ctx["schedule"] == [] and ctx["plan_grid"] is None
    manual = cost.compile_rows("Springfield EV", SPRINGFIELD_RATES, SPRINGFIELD_SCHEDULE)
    ctx = money_context(manual)
    assert ctx["plan_fits"] and ctx["plan_rates"] == SPRINGFIELD_RATES and ctx["schedule"] == SPRINGFIELD_SCHEDULE
    assert ctx["schedule_text"].startswith("peak 4pm–9pm")
    # a typed-in plan reads as its grid too: the grid is how every plan's hours are shown
    grid = ctx["plan_grid"]
    assert [g["title"] for g in grid["grids"]] == ["Every day"] and grid["grids"][0]["rows"][0]["cells"][17]["label"] == "Peak"
    assert [c["label"] for c in grid["now"]["prices"]] == ["Off-peak", "Part-peak", "Peak"] and grid["now"]["prices"][0]["price"] == "0.40"
    assert [r["title"] for r in ctx["rules"]] == ["Peak · 4 pm–9 pm", "Part-peak · 3 pm–4 pm", "Part-peak · 9 pm–10 pm"]
    assert ctx["rules"][2]["when"] == "Every day · Oct–May", "a run over the new year stays whole"
    assert money_context({"versions": "junk"})["plan"] is None, "a stored plan that does not parse shows as no plan"


def test_a_plan_the_rows_cannot_hold_shows_its_newest_grid_and_every_price():
    view = grid_view(_four())
    assert view["from"] == "2026-01-01" and view["note"] == "Plus $12 a month fixed."
    assert [g["title"] for g in view["grids"]] == ["Every day"] and len(view["grids"][0]["rows"]) == 12
    row = view["grids"][0]["rows"][6]
    assert row["month"] == "Jul" and len(row["cells"]) == 24
    assert row["cells"][10] == {"heat": 0.0, "label": "Super off-peak"} and row["cells"][17] == {"heat": 1.0, "label": "Peak"}
    assert [p["label"] for p in view["legend"]] == ["Super off-peak", "Off-peak", "Mid-peak", "Peak"], "cheapest first"
    assert [v["from"] for v in view["versions"]] == ["2026-01-01", "2024-01-01"], "newest first"
    assert view["versions"][1]["periods"][3] == {"field": "pv0_3", "label": "Peak", "price": 0.1 + 3 * 0.1}, "fields are named by the stored version's index"
    assert [g["title"] for g in grid_view(_four(weekend_same=False))["grids"]] == ["Weekdays", "Weekends"]
    assert money_context(_four())["plan_fits"] is False


def test_the_prices_in_force_today_are_marked_and_shown_plainly():
    rates = [{"from": "2025-01-01", "off_peak": 0.3, "part_peak": 0.4, "peak": 0.5}, {"from": "2030-01-01", "off_peak": 0.35, "part_peak": 0.45, "peak": 0.551}]
    ctx = money_context(cost.compile_rows("Springfield EV", rates, []), today=date(2026, 10, 5))
    assert [r["now"] for r in ctx["rate_rows"]] == [True, False], "a dated row not started yet is not the one in force"
    assert ctx["rate_rows"][1]["label"] == "Jan 1, 2030" and ctx["rate_rows"][1]["prices"] == "0.35 · 0.45 · 0.551"
    assert ctx["plan_grid"]["now"]["label"] == "Jan 1, 2025"
    assert money_context(cost.compile_rows("X", rates[1:], []), today=date(2026, 10, 5))["plan_grid"]["now"] is None
    view = grid_view(_four(), today=date(2026, 10, 5))
    assert [v["now"] for v in view["versions"]] == [True, False] and view["versions"][0]["summary"] == "0.20–0.50", "four prices read as a range"
    assert price_text(0.4) == "0.40" and price_text(0.301) == "0.301" and prices_summary([0.6, 0.4]) == "0.40 · 0.60"
    assert months_text(None) == "all year" and months_text([6, 7, 8, 9]) == "Jun–Sep" and months_text([1, 3]) == "Jan, Mar"

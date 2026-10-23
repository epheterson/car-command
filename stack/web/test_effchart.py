"""The efficiency-by-temperature chart: bands, bars of miles, the line of Wh a mile."""

import effchart


def row(lo, hi, miles, wh, drives=5):
    return {"lo": lo, "hi": hi, "miles": miles, "wh_per_mi": wh, "drives": drives}


def test_bands_are_five_degrees_in_the_shown_unit_and_open_at_both_ends():
    f = effchart.bands(False)
    assert f[1] == (-20, -15, -20) and f[0][0] < -100 and f[-1][1] > 200 and all(b[1] - b[0] == 5 for b in f[1:-1])
    c = effchart.bands(True)
    assert c[1][2] == -25 and c[1][0] == -13.0 and c[1][1] - c[1][0] == 9.0, "a 5 °C band, kept in °F for the drives"


def test_one_side_is_bars_of_miles_and_a_line_broken_where_a_band_had_no_drives():
    fb = effchart.bands(False)
    lo = lambda t: next(b for b in fb if b[2] == t)  # noqa: E731
    side = [row(*lo(40)[:2], 100.0, 320.0), row(*lo(45)[:2], 200.0, 300.0), row(*lo(55)[:2], 50.0, 280.0)]
    c = effchart.chart([side], fb)
    assert [s["shown"] for s in c["slots"]] == [40, 45, 50, 55], "from the first band driven to the last"
    assert len(c["bars"]) == 3 and max(b["h"] for b in c["bars"]) == effchart.H * effchart.BAR_TOP, "the most miles is the tallest bar"
    assert c["lines"][0]["d"].count("M") == 2, "no drives at 50–55: the line breaks there rather than bridging it"
    ys = {d["j"]: d["y"] for d in c["lines"][0]["dots"]}
    assert ys[0] < ys[1] < ys[3], "more Wh a mile is higher"
    assert c["ticks"][0]["v"] <= 280 and c["ticks"][-1]["v"] >= 320


def test_two_sides_sit_beside_each_other_on_one_scale():
    fb = effchart.bands(False)
    b40 = next(b for b in fb if b[2] == 40)[:2]
    b45 = next(b for b in fb if b[2] == 45)[:2]
    c = effchart.chart([[row(*b40, 100.0, 300.0), row(*b45, 80.0, 290.0)], [row(*b40, 50.0, 330.0)]], fb)
    a0, b0 = [b for b in c["bars"] if b["x"] < c["slots"][0]["w"]]
    assert a0["side"] == 0 and b0["side"] == 1 and b0["x"] > a0["x"] and b0["h"] < a0["h"]
    assert len(c["lines"]) == 2 and c["lines"][1]["d"].startswith("M") and "L" not in c["lines"][1]["d"]
    assert effchart.chart([[row(*b40, 100.0, 300.0)]], fb) is None, "one band is not a chart"

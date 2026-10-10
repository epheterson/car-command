import re
from datetime import datetime, timedelta, timezone

import pytest

from charts import line_chart


def test_empty_and_single_point_render_nothing():
    t = datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert line_chart([]) == "" and line_chart([(t, 50.0)]) == ""


def test_chart_has_axis_ticks_day_lines_and_all_points():
    t0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    series = [(t0 + timedelta(hours=6 * i), 40 + i) for i in range(12)]  # three days
    svg = line_chart(series, y_min=0, y_max=100, unit="%", label="Battery")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count('class="tick"') >= 3 + 2  # 3 y ticks + at least 2 day ticks
    assert "Sep 2" in svg and "Sep 3" in svg
    assert svg.count(",") >= 12  # every point is in the polyline
    assert 'aria-label="Battery"' in svg


def test_flat_series_does_not_divide_by_zero():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    svg = line_chart([(t0, 74.0), (t0 + timedelta(hours=1), 74.0)])
    assert "nan" not in svg and "inf" not in svg


def test_ticks_thin_out_with_the_span_and_the_axis_fits_wide_labels():
    from datetime import timedelta
    from charts import line_chart, time_ticks

    t0 = datetime(2022, 1, 4, 12, 0, tzinfo=timezone.utc)
    assert [x for _, x in time_ticks(t0, t0 + timedelta(days=5))] == ["Jan 5", "Jan 6", "Jan 7", "Jan 8", "Jan 9"]
    assert len(time_ticks(t0, t0 + timedelta(days=60))) == 8  # Mondays
    assert [x for _, x in time_ticks(t0, t0 + timedelta(days=400))][:2] == ["Feb", "Mar"]
    assert [x for _, x in time_ticks(t0, t0 + timedelta(days=4 * 365))] == ["2023", "2024", "2025", "2026"]
    svg = line_chart([(t0, 35.0), (t0 + timedelta(days=1400), 61880.0)], unit=" mi")
    assert "61,880 mi" in svg and svg.count('class="tick"') == 3 + 3  # 3 y labels + 3 year ticks
    x_axis = float(svg.split('<line class="grid" x1="')[1].split('"')[0])
    assert x_axis > 70  # room for "61,880 mi"


def test_bands_shade_behind_the_line_and_clip_to_the_span():
    """The Day page passes its drives and charges so a drop in the battery sits inside
    the drive that caused it. A band that starts before the series or ends after it is
    cut to the series; one wholly outside draws nothing."""
    t = lambda h: datetime(2026, 9, 14, h, tzinfo=timezone.utc)  # noqa: E731
    series = [(t(6), 80.0), (t(12), 70.0), (t(18), 60.0)]
    svg = line_chart(series, y_min=0, y_max=100, bands=[(t(8), t(9), "driving"), (t(16), t(20), "charging"), (t(1), t(2), "driving")])
    rects = re.findall(r'<rect class="band ([a-z]+)" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
    assert [k for k, _x, _w in rects] == ["driving", "charging"], "the band before the series must not draw"
    w_drive = float(rects[0][2])
    x_charge, w_charge = float(rects[1][1]), float(rects[1][2])
    assert w_drive > 0 and w_charge > 0
    # 16:00-20:00 clipped to 16:00-18:00 is two hours; 08:00-09:00 is one. Twice the width.
    assert w_charge == pytest.approx(2 * w_drive, rel=0.02)
    assert x_charge + w_charge <= 720 - 12 + 0.1, "the clipped band ends at the right edge, not past it"
    assert svg.index("<rect") < svg.index("<polyline"), "bands draw under the line, not over it"


def test_no_bands_changes_nothing():
    t = lambda h: datetime(2026, 9, 14, h, tzinfo=timezone.utc)  # noqa: E731
    series = [(t(6), 80.0), (t(18), 60.0)]
    assert line_chart(series, y_min=0, y_max=100) == line_chart(series, y_min=0, y_max=100, bands=[])
    assert "<rect" not in line_chart(series, y_min=0, y_max=100, bands=None)

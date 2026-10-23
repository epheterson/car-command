"""Tiny SVG line charts. Pure: series in, markup string out. No library, no JS."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

import clockfmt


def line_chart(
    series: list[tuple[datetime, float]],
    *,
    width: int = 720,
    height: int = 160,
    y_min: float | None = None,
    y_max: float | None = None,
    unit: str = "",
    tz=None,
    label: str = "",
    bands: list[tuple[datetime, datetime, str]] | None = None,
) -> str:
    """One series over time with a left axis (3 ticks) and day ticks along the bottom.

    `bands` are (start, end, kind) stretches shaded behind the line — the Day page passes
    its drives and charges, so a drop in the battery line sits inside the blue of the
    drive that caused it rather than needing to be matched against the rows below by
    eye. Clipped to the series' span; a band wholly outside it draws nothing.

    Returns '' when there are fewer than two points, so templates can show a
    plain sentence instead of an empty box.
    """
    if len(series) < 2:
        return ""
    pad_r, pad_t, pad_b = 12, 10, 22
    xs = [t for t, _ in series]
    ys = [v for _, v in series]
    t0, t1 = xs[0], xs[-1]
    span = (t1 - t0).total_seconds() or 1.0
    lo = min(ys) if y_min is None else y_min
    hi = max(ys) if y_max is None else y_max
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    # the left margin fits the widest axis label ("95,358 mi" needs more room than "100%")
    pad_l = 12 + 7 * max(len(f"{v:,.0f}{unit}") for v in (lo, hi, lo + (hi - lo) / 2))
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def X(t: datetime) -> float:
        return pad_l + (t - t0).total_seconds() / span * iw

    def Y(v: float) -> float:
        return pad_t + (1 - (v - lo) / (hi - lo)) * ih

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">']
    # Bands first, so grid, line and ticks all draw over them.
    for a, b, kind in bands or []:
        a, b = max(a, t0), min(b, t1)
        if b <= a:
            continue
        parts.append(f'<rect class="band {escape(kind)}" x="{X(a):.1f}" y="{pad_t}" width="{X(b) - X(a):.1f}" height="{ih}"/>')
    for frac in (0.0, 0.5, 1.0):
        v = lo + (hi - lo) * frac
        y = Y(v)
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{v:,.0f}{escape(unit)}</text>')
    for when, text in time_ticks(t0, t1, tz):
        x = X(when)
        parts.append(f'<line class="grid v" x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{height - pad_b}"/>')
        parts.append(f'<text class="tick" x="{x:.1f}" y="{height - 6}" text-anchor="middle">{text}</text>')
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in series)
    parts.append(f'<polyline class="line" points="{pts}"/>')
    lx, ly = X(t1), Y(ys[-1])
    parts.append(f'<circle class="end" cx="{lx:.1f}" cy="{ly:.1f}" r="3.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def time_ticks(t0: datetime, t1: datetime, tz=None) -> list[tuple[datetime, str]]:
    """Bottom-axis ticks that suit the span: days up to two weeks, weeks up to
    three months, months up to two years, then years. Never more than ~16."""
    local0 = t0.astimezone(tz) if tz else t0
    days = (t1 - t0).total_seconds() / 86400
    ticks: list[tuple[datetime, str]] = []
    if days <= 15:
        d = local0.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        while d < t1:
            ticks.append((d, d.strftime(clockfmt.adapt("%b %-d"))))
            d += timedelta(days=1)
    elif days <= 100:
        d = local0.replace(hour=0, minute=0, second=0, microsecond=0)
        d += timedelta(days=(7 - d.weekday()) % 7 or 7)  # next Monday
        while d < t1:
            ticks.append((d, d.strftime(clockfmt.adapt("%b %-d"))))
            d += timedelta(days=7)
    elif days <= 800:
        y, m = local0.year, local0.month + 1
        while True:
            if m > 12:
                y, m = y + 1, 1
            d = local0.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)
            if d >= t1:
                break
            ticks.append((d, d.strftime("%b %y") if days > 400 else d.strftime("%b")))
            m += 1
    else:
        y = local0.year + 1
        while True:
            d = local0.replace(year=y, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            if d >= t1:
                break
            ticks.append((d, str(y)))
            y += 1
    return ticks

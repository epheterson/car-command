"""What the Stats page says about a month: its figures beside the month before and the
twelve before that (span for span while it is still running), the bars for a year of
months, the totals for a window, and the range and compare controls, all from the month
rows summarize_sql already returns. Pure."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

SUMS = ("miles", "drives", "added_kwh", "cost", "used_kwh", "drive_h", "charges", "priced", "avg_cost")
METRICS = (("miles", "Driven"), ("drives", "Drives"), ("added_kwh", "Charged"), ("cost", "Charging cost"), ("cost_per_mi", "Per mile"))
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def shift(ym: tuple[int, int], n: int) -> tuple[int, int]:
    y, m = ym
    m += n
    return (y + (m - 1) // 12, (m - 1) % 12 + 1)


def by_month(rows: list[dict]) -> dict[tuple, dict]:
    return {(r["period"].year, r["period"].month): r for r in rows}


def fetch_from(focus: tuple) -> tuple:
    """The first month to fetch for a focus month: thirteen before it, so that when the
    month the fetch opens on is skipped as a possible stub, twelve whole months remain."""
    return shift(focus, -13)


GAP = 3  # this many empty months in a row and the car was not being logged, or not yet had


def _logged(m: dict | None) -> bool:
    return bool(m) and bool((m.get("drives") or 0) or (m.get("charges") or 0) or (m.get("miles") or 0))


def history_start(months: dict[tuple, dict], focus: tuple | None = None) -> tuple | None:
    """The first month of the car's logged history as it runs up to `focus`: the first
    logged month after the last stretch of GAP or more empty months. A stray row from long
    before the car arrived (an import's first line, a previous owner's last) would otherwise
    start its history there, and every empty month after it would average in as a zero.
    A month or two parked with nothing logged stays a zero."""
    logged = sorted(ym for ym, m in months.items() if _logged(m) and (focus is None or ym <= focus))
    if not logged:
        return None
    start = logged[0]
    for a, b in zip(logged, logged[1:]):
        gap = (b[0] - a[0]) * 12 + b[1] - a[1] - 1
        if gap >= GAP:
            start = b
    return start


def first_full(months: dict[tuple, dict], focus: tuple | None = None) -> tuple | None:
    """The first month to compare against: the one after the first month of the car's
    history (history_start), which is nearly always partial (logging begins on some day)."""
    start = history_start(months, focus)
    return shift(start, 1) if start else None


def add(a: dict | None, b: dict | None) -> dict:
    """Two month rows summed (either may be missing), with the rates worked out again."""
    out = {k: ((a or {}).get(k) or 0) + ((b or {}).get(k) or 0) for k in SUMS}
    out["cost_per_mi"] = out["cost"] / out["miles"] if out["miles"] > 1 else None
    out["wh_per_mi"] = out["used_kwh"] * 1000 / out["miles"] if out["miles"] > 1 and out["used_kwh"] > 0 else None
    return out


def _value(month: dict | None, key: str):
    return add(month, None)[key]


def _change(a, b):
    return (a - b) / b if a is not None and b else None


def _total(months: dict[tuple, dict], keys: list[tuple]) -> dict | None:
    total = None
    for ym in keys:
        total = add(total, months.get(ym))
    return total


def cost_mark(key: str, month: dict | None) -> str | None:
    """'none' for a cost over charges none of which has a price, 'est' for one that is
    partly a regional-average estimate, else None. Only the cost metrics carry one."""
    if key not in ("cost", "cost_per_mi") or not month:
        return None
    m = add(month, None)
    if m["charges"] and not m["priced"]:
        return "none"
    return "est" if m["avg_cost"] > 0 else None


PACE_MIN = 2 / 31  # under two days in, a month's pace says nothing yet


def month_days(ym: tuple) -> int:
    return (date(*shift(ym, 1), 1) - date(*ym, 1)).days


def span_label(ym: tuple, days: int) -> str:
    """'Oct 1–5': the first `days` days of a month, cut to that month's own length."""
    d = min(days, month_days(ym))
    return f"{MONTHS[ym[1] - 1]} {d}" if d == 1 else f"{MONTHS[ym[1] - 1]} 1–{d}"


def compare(months: dict[tuple, dict], focus: tuple, fraction: float, first: tuple, like: dict[tuple, dict] | None = None) -> list[dict]:
    """Each metric for the focus month beside the month before and the average of up to
    twelve before it, counting only months from `first` on (a car bought this spring is
    not averaged over months it did not exist).

    A month in progress (`fraction` < 1) is set beside the same span of the others:
    `like` holds every month cut to as far in as the focus month has got, so Oct 1–5 is
    read against Sep 1–5 and the average of each month's 1–5. Its full month is projected
    at the pace so far (`proj`), a side figure that is never the one compared. A whole
    month compares whole to whole."""
    partial = fraction < 1 and like is not None
    src = like if partial else months
    prev = shift(focus, -1)
    history = [shift(focus, -i) for i in range(1, 13) if shift(focus, -i) >= first]
    out = []
    for key, label in METRICS:
        this = _value(src.get(focus), key)
        last = _value(src.get(prev), key) if prev >= first else None
        proj = None  # a rate has no full-month figure to reach
        if key == "cost_per_mi":
            total = _total(src, history)
            avg = total["cost_per_mi"] if total else None
        else:
            avg = sum(_value(src.get(ym), key) for ym in history) / len(history) if history else None
            if partial and fraction >= PACE_MIN:
                proj = this / fraction
        vs_avg = _change(this, avg)
        out.append(
            {
                "key": key,
                "label": label,
                # Per column (this month, last, the average): "est" where part of the cost is
                # an estimate at a regional average, "none" where there were charges and not
                # one has a price yet, so a bare 0 is not read as free.
                "marks": [cost_mark(key, m) for m in (src.get(focus), src.get(prev) if prev >= first else None, _total(src, history))],
                "this": this,
                "last": last,
                "avg": avg,
                "proj": proj,
                "n_avg": len(history),
                "vs_last": _change(this, last),
                "vs_avg": vs_avg,
                "vbar": vbar(vs_avg),
            }
        )
    return out


def heads(focus: tuple, fraction: float, day: int | None) -> dict:
    """The comparison's column names: whole months, or the same span of each."""
    prev = shift(focus, -1)
    if fraction < 1 and day:
        return {"this": span_label(focus, day), "last": span_label(prev, day), "avg": f"12-mo avg, {'day 1' if day == 1 else f'1–{day}'}", "partial": True}
    return {"this": MONTHS[focus[1] - 1], "last": MONTHS[prev[1] - 1], "avg": "12-mo avg", "partial": False}


VBAR_CAP = 1.0  # ±100 % fills half the track; past it the bar stops at the end, capped


def vbar(change: float | None) -> dict | None:
    """A change drawn out from the middle of its track: `w` is the share of the half it
    fills (0–100), `cap` marks one that ran past the scale and was stopped at the end."""
    if change is None:
        return None
    return {"side": "up" if change > 0 else "down" if change < 0 else "flat", "w": round(min(abs(change), VBAR_CAP) / VBAR_CAP * 100, 1), "cap": abs(change) > VBAR_CAP}


def bars(
    car_months: dict[str, dict[tuple, dict]],
    order: list[str],
    focus: tuple,
    first: tuple,
    units,
    n: int = 12,
    w: int = 720,
    h: int = 210,
    short: bool = False,
    proj: float | None = None,
    since: tuple | None = None,
) -> dict:
    """SVG geometry for n months of distance ending at the focus, stacked by car in
    `order`, in the active units, with the average of the months before the focus.
    A month in progress carries `proj`, its whole-month miles at the pace so far, drawn
    as a lighter extension above what it has so far.
    A phone gets its own narrow drawing (`short`: one-letter months, no unit on the axis)
    so its text is not scaled down to nothing."""
    from chartdata import nice

    L, R, T, B = (40 if short else 62), 12, 14, 24  # room for the top label, with its unit ("3,000 mi") when wide
    # only the months the car has been logged (`since`, history_start): no empty slots for
    # the months before it arrived, drawn as if it had stood still
    keys = [ym for ym in (shift(focus, -i) for i in range(n - 1, -1, -1)) if since is None or ym >= since]
    n = max(len(keys), 6)
    dist = lambda mi: units.distance(mi) or 0.0
    totals = [sum(dist(_value(car_months[v].get(ym), "miles")) for v in order) for ym in keys]
    history = [t for ym, t in zip(keys, totals) if ym != focus and ym >= first]
    avg = sum(history) / len(history) if history else None
    pace = dist(proj) if proj else None
    _, top = nice([0, max([*totals, avg or 0, pace or 0, 1.0])])
    ph = h - T - B
    slot = (w - L - R) / n
    bw = slot * 0.62
    out = []
    for i, (ym, total) in enumerate(zip(keys, totals)):
        x = L + i * slot + (slot - bw) / 2
        # Round the shared edges, not each segment, so one car's top is exactly the next one's
        # bottom and no hairline seam shows between them.
        y, edge, segs = T + ph, round(T + ph, 1), []
        for v in order:
            y -= dist(_value(car_months[v].get(ym), "miles")) / top * ph
            upper = round(y, 1)
            segs.append({"y": upper, "h": round(edge - upper, 1), "vin": v})
            edge = upper
        bar = {"x": round(x, 1), "w": round(bw, 1), "label": MONTHS[ym[1] - 1][0] if short else MONTHS[ym[1] - 1], "value": total, "focus": ym == focus, "segs": segs, "top": edge, "proj": None}
        if ym == focus and pace and pace > total:
            # the stack's round cap moves up to the projection's top; what it has so far stays square under it
            p = round(T + ph - pace / top * ph, 1)
            bar["proj"] = {"y": p, "h": round(edge - p, 1), "value": pace}
        out.append(bar)
    grid = [{"y": round(T + ph - f * ph, 1), "value": top * f} for f in (0, 0.5, 1)]
    legend = [{"vin": v, "total": sum(dist(_value(car_months[v].get(ym), "miles")) for ym in keys if ym != focus and ym >= first)} for v in order]
    return {
        "w": w,
        "h": h,
        "short": short,
        "L": L,
        "R": R,
        "base": T + ph,
        "bars": out,
        "grid": grid,
        "legend": legend,
        "avg": {"value": avg, "y": round(T + ph - avg / top * ph, 1)} if avg is not None else {"value": None, "y": None},
    }


def window_sum(rows: list[dict], since, until) -> dict:
    """The month rows from `since` (a month's first day) up to `until`, added up."""
    total = None
    for r in rows:
        if (since is None or r["period"] >= since) and (until is None or r["period"] < until):
            total = add(total, r)
    return total or add(None, None)


def month_aligned(dt: datetime | None) -> bool:
    """Whether month rows can be cut at `dt` (None, or local midnight on a first)."""
    return dt is None or (dt.day == 1 and dt.time() == datetime.min.time())


# The page's one period control: a month, a year (each with its stepper), all time, or two
# dates; then the comparisons and the grains, each as (key, label[, short label]).
PERIODS = (("month", "Month"), ("year", "Year"), ("all", "All"), ("custom", "Custom"))
PRESETS = (("month", "This month"), ("last-month", "Last month"), ("30d", "30 days"), ("year", "This year"), ("last-year", "Last year"), ("all", "All"), ("custom", "Custom"))
VERSUS = (("none", "None", "None"), ("prev", "Previous period", "Previous"), ("yoy", "Same period last year", "Last year"))
GRAINS = ("day", "week", "month")
OVER = (1, 5, 10, 25, 50)
DEFAULTS = {"period": "month", "range": "year", "vs": "none", "by": "week", "over": "1"}


def back(dt: datetime, months: int) -> datetime:
    """`dt` moved back whole calendar months, its day cut to the shorter month's length."""
    y, m = shift((dt.year, dt.month), -months)
    return dt.replace(year=y, month=m, day=min(dt.day, month_days((y, m))))


def period_label(since: datetime | None, end: datetime) -> str:
    """'Oct 1–5, 2026' for [since, end): the days it touches, end exclusive."""
    if since is None:
        return "All time"
    a, b = since.date(), (end - timedelta(microseconds=1)).date()
    if b < a:
        b = a
    md = lambda d: f"{MONTHS[d.month - 1]} {d.day}"
    if a == b:
        return f"{md(a)}, {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{md(a)}–{b.day}, {a.year}"
    if a.year == b.year:
        return f"{md(a)} – {md(b)}, {a.year}"
    return f"{md(a)}, {a.year} – {md(b)}, {b.year}"


def resolve(key: str | None, d0: date | None, d1: date | None, now: datetime) -> dict:
    """[since, until) in local time for a range preset; `until` None runs up to `now`
    (local, aware), and such a range is in progress. Dates alone, without a preset, are a
    custom range; nothing at all is this year."""
    keys = [k for k, _ in PRESETS]
    key = key if key in keys else ("custom" if d0 or d1 else DEFAULTS["range"])
    tz, today = now.tzinfo, now.date()
    mid = lambda d: datetime(d.year, d.month, d.day, tzinfo=tz)
    this_month, this_year = mid(today.replace(day=1)), mid(today.replace(month=1, day=1))
    since, until, step = {
        "month": (this_month, None, 1),
        "last-month": (back(this_month, 1), this_month, 1),
        "30d": (mid(today - timedelta(days=29)), None, None),
        "year": (this_year, None, 12),
        "last-year": (back(this_year, 12), this_year, 12),
        "all": (None, None, None),
        "custom": (mid(d0) if d0 else None, mid(d1 + timedelta(days=1)) if d1 and d1 < today else None, None),
    }[key]
    end = until or now
    return {"key": key, "since": since, "until": until, "end": end, "step": step, "in_progress": until is None, "label": period_label(since, end)}


def versus(r: dict, vs: str | None) -> dict | None:
    """The range to set beside `r`: the one before it ('prev') or the same dates a year
    back ('yoy'). A range still running is matched span for span: this month so far
    against the same days of last month, not against all of it."""
    since, end, step = r["since"], r["end"], r["step"]
    if vs not in ("prev", "yoy") or since is None:
        return None
    if vs == "yoy" or step == 12:
        s, e = back(since, 12), back(end, 12)
    elif step:  # a calendar month: the month before, cut to the same span when this one is running
        s = back(since, step)
        e = since if r["until"] else min(s + (end - since), since)
    else:
        s, e = since - (end - since), since
    return {"since": s, "until": e, "label": period_label(s, e)}


def _ym(text: str | None) -> tuple | None:
    try:
        y, m = (int(x) for x in (text or "").split("-"))
        return (y, m) if 1 <= m <= 12 and 1900 < y < 3000 else None
    except ValueError:
        return None


def period(key: str | None, ym: str | None, year: str | None, d0: date | None, d1: date | None, now: datetime) -> dict:
    """The window the whole page is about, from its one control: a calendar month (`ym`,
    "2026-09"; this month by default), a calendar year, all time, or two dates. The old
    range presets still open where they meant (Last month is September, 30 days two dates).
    [since, until) in local time; `until` None runs up to `now`. Carries its stepper:
    the period before and after, the next one None while this one is still running."""
    tz, today = now.tzinfo, now.date()
    legacy = {"last-month": "month", "last-year": "year", "30d": "custom"}
    key = key if key in [k for k, _ in PERIODS] else legacy.get(key or "", "custom" if d0 or d1 else "month")
    if key == "custom":
        if d0 is None and d1 is None:
            d0 = today - timedelta(days=29)  # "30 days", from before there was a Custom
        r = resolve("custom", d0, d1, now)
        return {**r, "now_label": r["label"], "prev": None, "next": None}
    if key == "all":
        r = resolve("all", None, None, now)
        return {**r, "now_label": "All time", "prev": None, "next": None}
    if key == "year":
        try:
            y = int(year) if year else None
        except ValueError:
            y = None
        y = y if y and 1900 < y <= today.year else today.year
        since = datetime(y, 1, 1, tzinfo=tz)
        until = datetime(y + 1, 1, 1, tzinfo=tz) if y < today.year else None
        end = until or now
        return {
            "key": "year",
            "since": since,
            "until": until,
            "end": end,
            "step": 12,
            "in_progress": until is None,
            "label": period_label(since, end),
            "year": y,
            "now_label": str(y),
            "prev": str(y - 1),
            "prev_label": str(y - 1),
            "next": (None if y + 1 == today.year else str(y + 1)) if until else None,
            "next_ok": until is not None,
            "next_label": str(y + 1),
        }
    cur = (today.year, today.month)
    at = _ym(ym) or cur
    at = min(at, cur)
    since = datetime(at[0], at[1], 1, tzinfo=tz)
    nxt = shift(at, 1)
    until = datetime(nxt[0], nxt[1], 1, tzinfo=tz) if at < cur else None
    end = until or now
    prev = shift(at, -1)
    return {
        "key": "month",
        "since": since,
        "until": until,
        "end": end,
        "step": 1,
        "in_progress": until is None,
        "label": period_label(since, end),
        "ym": f"{at[0]}-{at[1]:02d}",
        "now_label": f"{date(*at, 1):%B %Y}",
        "prev": f"{prev[0]}-{prev[1]:02d}",
        "prev_label": f"{MONTHS[prev[1] - 1]} {prev[0]}",
        "next": (None if nxt == cur else f"{nxt[0]}-{nxt[1]:02d}") if until else None,
        "next_ok": until is not None,
        "next_label": f"{MONTHS[nxt[1] - 1]} {nxt[0]}",
    }


def legacy_state(key: str | None, now: datetime) -> dict:
    """An old range preset as the period and its place: Last month is the month before
    this one, Last year the year before; the rest need nothing."""
    if key == "last-month":
        y, m = shift((now.year, now.month), -1)
        return {"period": "month", "month": f"{y}-{m:02d}"}
    if key == "last-year":
        return {"period": "year", "year": str(now.year - 1)}
    return {}


def controls(state: dict, r: dict) -> dict:
    """The period control, its stepper, and the compare and grain choices, as ready query
    strings. `state` is the page's URL state (period, month, year, from, to, vs, by, over,
    vin); a link changes one part of it and keeps the rest, and leaves defaults out so a
    bare /stats stays bare. A month keeps no year, a year no month."""

    def href(**change) -> str:
        m = {**state, **change}
        if m.get("period") != "custom":
            m["from"] = m["to"] = None
        if m.get("period", "month") != "month":
            m["month"] = None
        if m.get("period") != "year":
            m["year"] = None
        return urlencode([(k, v) for k, v in m.items() if v not in (None, "") and DEFAULTS.get(k) != str(v)])

    since, end = r["since"], r["end"]
    custom = {"period": "custom", "from": since.date().isoformat() if since else None, "to": (end - timedelta(microseconds=1)).date().isoformat()}
    key = r["key"]
    step = None
    if key in ("month", "year"):
        part = "month" if key == "month" else "year"
        step = {
            "label": r["now_label"],
            "prev": href(**{part: r["prev"]}),
            "prev_label": r["prev_label"],
            # forward to the period still running leaves the URL bare of it
            "next": href(**{part: r["next"]}) if r.get("next_ok") else None,
            "next_label": r["next_label"],
        }
    return {
        "periods": [{"key": k, "label": lbl, "on": key == k, "href": href(**custom) if k == "custom" else href(period=k, month=None, year=None)} for k, lbl in PERIODS],
        "step": step,
        # a month is already set beside the month before and its average; all time has nothing before it
        "versus": (
            [{"key": k, "label": lbl, "short": sh, "on": (state.get("vs") or "none") == k, "href": href(vs=k)} for k, lbl, sh in VERSUS if not (key == "year" and k == "yoy")]
            if key in ("year", "custom")
            else []
        ),
        "grains": [{"key": k, "label": k.capitalize(), "on": state.get("by") == k, "href": href(by=k)} for k in GRAINS],
        "custom": key == "custom",
        "from": since.date().isoformat() if since and key == "custom" else "",
        "to": state.get("to") or "",
        "keep": [(k, v) for k, v in state.items() if k not in ("period", "from", "to", "month", "year") and v not in (None, "") and DEFAULTS.get(k) != str(v)],
        "href": href,
    }

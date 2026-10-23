"""The one place the clock and date-order settings turn a strftime pattern into the pattern a person chose.

Templates reach it through the `local` and `dfmt` filters; Python that words a time (an alert text, a chart
tick, a command's answer) calls `text`. PREFS is set when settings load, so none of those takes a setting
as an argument."""

from __future__ import annotations

import re

PREFS = {"clock": "12", "date_order": "mdy"}


def adapt(fmt: str, clock: str | None = None, date_order: str | None = None) -> str:
    """A strftime pattern rewritten for the clock (12 or 24) and date order (mdy or dmy) chosen on Settings, so
    every caller keeps asking for the familiar pattern and gets the chosen one."""
    clock = clock or PREFS["clock"]
    date_order = date_order or PREFS["date_order"]
    if clock == "24":
        fmt = fmt.replace("%-I:%M:%S %p", "%H:%M:%S").replace("%I:%M:%S %p", "%H:%M:%S")
        fmt = fmt.replace("%-I:%M %p", "%H:%M").replace("%I:%M %p", "%H:%M").replace("%-I %p", "%H:00").replace("%-I:%M", "%H:%M")
    if date_order == "dmy":
        fmt = fmt.replace("%b %-d, %Y", "%-d %b %Y").replace("%b %-d %Y", "%-d %b %Y").replace("%B %-d, %Y", "%-d %B %Y")
        fmt = fmt.replace("%B %-d", "%-d %B").replace("%b %-d", "%-d %b").replace("%m/%d/%Y", "%d/%m/%Y").replace("%m/%d", "%d/%m")
    return fmt


def tied(text: str) -> str:
    """A 12-hour time with a no-break space before AM or PM, so “4:16 PM” never splits across lines."""
    return re.sub(r"(\d) ([AP]M)\b", "\\1\u00a0\\2", text)


def text(dt, pattern: str) -> str:
    """`dt` worded by `pattern` in the chosen clock and date order (no zone change: pass a local time)."""
    return dt.strftime(adapt(pattern))


def tod(value, clock: str | None = None) -> str:
    """A stored "HH:MM" as the chosen clock says it: "9:00 PM" (no leading zero) or "21:00" (always two digits)."""
    parsed = parse_tod(value)
    if parsed is None:
        return ""
    h, m = parsed
    if (clock or PREFS["clock"]) == "24":
        return f"{h:02d}:{m:02d}"
    return f"{(h + 11) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def hour_label(h: int, clock: str | None = None) -> str:
    """A whole hour of the day, 0 to 24, as the chosen clock says it: "6 AM", "12 PM", "midnight" at 24 in neither."""
    h %= 24
    if (clock or PREFS["clock"]) == "24":
        return f"{h:02d}:00"
    return f"{(h + 11) % 12 + 1} {'AM' if h < 12 else 'PM'}"


_TOD = re.compile(r"^\s*(\d{1,2})(?:[:.h]?(\d{2}))?\s*([ap])?\.?m?\.?\s*$", re.IGNORECASE)


def parse_tod(text) -> tuple[int, int] | None:
    """A time of day as a person types it ("21:00", "9:30 pm", "9pm", "0930", "7") into (hour, minute), or None. 12 AM is 0 and 12 PM is
    noon; a bare hour with no AM or PM is read as typed (a 24-hour clock)."""
    got = _TOD.match(str(text)) if text is not None else None
    if not got:
        return None
    h, m, ap = int(got.group(1)), int(got.group(2) or 0), (got.group(3) or "").lower()
    if m > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "p" else 0)
    elif h > 23:
        return None
    return h, m

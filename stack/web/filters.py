"""Jinja filters the templates rely on. Registered by the app, the tests and the build-time template check alike."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jinja2 import pass_context

import identity
import units as units_mod
from averages import month_label
from placeicons import COMMON_KEYS, ICONS_VERSION, PLACE_ICONS, QUICK_KEYS
from placeicons import KIND_ICONS, NAME_ICONS
from placeicons import resolve_icon as _resolve_icon
from placeicons import svg as _place_svg
from placeicons import title as _icon_title
from places import RADIUS_MAX, RADIUS_MIN
from units import humanize_duration


def est_mark(flag, why: str = "estimate"):
    """'≈' in a span whose title says what the estimate rests on, for a cost priced on a
    regional average; nothing for a cost that is not one."""
    from markupsafe import Markup, escape

    return Markup(f'<span class="est" title="{escape(why)}">≈</span>') if flag else Markup("")


def adapt(fmt: str, clock: str = "12", date_order: str = "mdy") -> str:
    """A strftime pattern rewritten for the clock and date order chosen on Settings, so
    every template keeps asking for the familiar pattern and gets the chosen one."""
    if clock == "24":
        fmt = fmt.replace("%-I:%M %p", "%H:%M").replace("%I:%M %p", "%H:%M").replace("%-I %p", "%H:00")
    if date_order == "dmy":
        fmt = fmt.replace("%b %-d, %Y", "%-d %b %Y").replace("%b %-d %Y", "%-d %b %Y").replace("%b %-d", "%-d %b").replace("%m/%d/%Y", "%d/%m/%Y").replace("%m/%d", "%d/%m")
    return fmt


@pass_context
def _places_filter(context, text):
    """The `places` filter, reading `place_icon_map` when there is one.

    It is registered as a Jinja *global* (below), not a per-request context variable:
    `context.get(...)` sees globals wherever the filter runs, including a macro imported
    without context (_parts.html's `row`/`gap_row`/`car_card`, used by garage.html,
    now.html, drives.html, charges.html, day.html, stats.html, timeline.html,
    battery.html) and a card drawn straight from the live stream with no request context
    at all — a per-request context variable reaches neither. A bare
    `env.from_string().render()` with nothing registered for it — every pure-template
    test — gets the Home/Work defaults only, with no database involved.
    """
    return places(text, context.get("place_icon_map"))


def register(env, tz: ZoneInfo | None = None, clock: str = "12", date_order: str = "mdy", place_icons=None) -> None:
    tz = tz or ZoneInfo("America/Los_Angeles")
    env.filters["local"] = lambda dt, fmt="%a %b %-d, %-I:%M %p": dt.astimezone(tz).strftime(adapt(fmt, clock, date_order)) if dt else ""
    env.filters["when"] = lambda dt: when(dt, tz, clock=clock, date_order=date_order)
    env.filters["short_place"] = short_place
    env.filters["places"] = _places_filter
    env.globals["place_refs"] = _place_refs_global  # place names that open the naming card
    env.filters["place_road"] = place_road
    env.filters["place_city"] = place_city
    env.filters["short_address"] = short_address
    # the icon picker (places.html) and the `places` filter read the same set, defined
    # once in placeicons.py
    env.globals["PLACE_ICONS"] = PLACE_ICONS
    # the picker draws the common ones into the page and fetches the rest, cached, from
    # /icons/places.json?v=PLACE_ICONS_V when it searches or shows more
    env.globals["PLACE_ICON_COMMON"] = COMMON_KEYS
    env.globals["PLACE_ICON_QUICK"] = QUICK_KEYS  # the icon row's first few, before More…
    env.globals["PLACE_ICONS_V"] = ICONS_VERSION
    env.globals["place_icon_title"] = _icon_title
    env.globals["RADIUS_MIN"], env.globals["RADIUS_MAX"] = RADIUS_MIN, RADIUS_MAX  # a place's size slider
    # the picker's closed-face button shows the icon a place would actually render with,
    # defaults included, not just what is explicitly stored
    env.globals["resolve_place_icon"] = _resolve_icon
    # the place picker's icon guesses, from a typed name or a nearby place's kind
    env.globals["PLACE_NAME_ICONS"] = NAME_ICONS
    env.globals["PLACE_KIND_ICONS"] = KIND_ICONS
    if place_icons is not None:
        # a callable (the app's cached, fast-failing place_icon_map); a global, not a
        # per-request context value, so it reaches every render path |places runs in
        env.globals["place_icon_map"] = place_icons
    env.filters["num"] = lambda x, nd=0: "" if x is None else f"{x:,.{nd}f}"
    env.filters["month_label"] = month_label  # "2026-07" -> "Jul 2026"
    env.globals["est"] = est_mark
    env.filters["duration"] = lambda d: humanize_duration(d) if isinstance(d, timedelta) or d is None else str(d)
    # Quantities that have a unit. Each converts from the stored imperial value into
    # whatever system is active and formats it; the label lives in the template context
    # as `u`, because the markup often puts it in its own element.
    env.filters["dist"] = lambda x, nd=0: _fmt(units_mod.ACTIVE.distance(_n(x)), nd)
    env.filters["speed"] = lambda x, nd=0: _fmt(units_mod.ACTIVE.speed(_n(x)), nd)
    env.filters["temp"] = lambda x, nd=0: _fmt(units_mod.ACTIVE.temperature(_n(x)), nd)
    env.filters["econ"] = lambda x, nd=0: _fmt(units_mod.ACTIVE.per_distance(_n(x)), nd)
    env.filters["perdist"] = lambda x, nd=0: _fmt(units_mod.ACTIVE.per_distance(_n(x)), nd)
    env.filters["elev"] = lambda x, nd=0: _fmt(units_mod.ACTIVE.elevation(_n(x)), nd)
    env.filters["tread"] = lambda mm: units_mod.tread_display(_n(mm), units_mod.ACTIVE.metric)
    env.globals["u"] = _Labels()
    # the shell's navigation, the same tree the app routes by (nav.py)
    import nav

    env.globals["nav_sections"] = nav.SECTIONS
    env.globals["nav_settings"] = nav.SETTINGS
    env.globals["nav_phone_tabs"] = nav.PHONE_TABS
    env.globals["section_of"] = nav.section_of
    env.globals["car_href"] = nav.car_url
    import nowpage

    env.globals["state_word"] = nowpage.state_word
    env.globals["gauge_of"] = nowpage.gauge
    import daypage

    # the Day rows read in the active units; units_mod.ACTIVE is looked up at call time, so a
    # Settings change applies without re-registering
    env.globals["day_row"] = lambda r, cur="$": daypage.row_parts(r, units_mod.ACTIVE, cur)
    env.globals["day_gap"] = lambda r, where=None, guard=None: daypage.gap_text(r, where, units_mod.ACTIVE, guard)
    env.globals["guard_name"] = identity.guard_name
    # a page overrides these with its cars' own names; a bare render falls back to the generic one
    env.globals["guard"] = {}
    env.globals["guard_all"] = identity.guard_name(None)
    env.globals["day_detail"] = lambda r, cur="$": daypage.detail_line(r, units_mod.ACTIVE, cur)
    env.globals["as_drive"] = daypage.as_drive
    env.globals["as_charge"] = daypage.as_charge


def _n(x) -> float | None:
    """A number, or nothing. A key missing from the context arrives as jinja2.Undefined,
    which is not None and raises on arithmetic; units.py stays pure by not knowing that."""
    try:
        return float(x)
    except Exception:  # noqa: BLE001 - jinja2.UndefinedError is not a TypeError
        return None


def _fmt(x, nd: int) -> str:
    # A key missing from the context arrives as jinja2.Undefined, which is not None and
    # would raise on format. A page with one blank figure beats a page that 500s.
    try:
        return f"{x:,.{nd}f}"
    except (TypeError, ValueError):
        return ""


class _Labels:
    """`u.dist` and friends, read at render time so a settings change needs no re-register."""

    def __getattr__(self, name: str) -> str:
        return units_mod.ACTIVE.labels.get(name, "")

    def __getitem__(self, name: str) -> str:
        return getattr(self, name)


def when(dt, tz, now=None, clock: str = "12", date_order: str = "mdy") -> str:
    """A time the way a person says it: today by the clock, this week by the day, older by the date.
    Which pattern comes from the distance to now; the clock and date order from Settings."""
    if not dt:
        return ""
    f = lambda pat: local.strftime(adapt(pat, clock, date_order))
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    local = dt.astimezone(tz)
    days = (now.date() - local.date()).days
    if days == 0:
        return f("%-I:%M %p")
    if days == 1:
        return f("Yesterday %-I:%M %p")
    if 0 < days < 7:
        return f("%a %-I:%M %p")
    if local.year == now.year:
        return f("%b %-d, %-I:%M %p")
    return f("%b %-d %Y, %-I:%M %p")


# A place's icon gets a small glyph before its name where that name is the whole word
# ("Home" in "Parked 18 min at Home", not inside "Home Depot"). The icons live once in
# placeicons.py; this just finds the word and decides which glyph, if any, it gets.
# Nothing word-like before it, and after it the end, an arrow, a separator or a number
# ("Home 9.4 h"). So "Home Depot" and "Network Plaza" stay plain.
_WORD_TAIL = r"(?=$|\s*[→·,(]|\s+\d)"


def _place_pattern(escaped_names: set[str]):
    # Each name is already HTML-escaped (for the text it will match against), but that
    # leaves regex metacharacters — "Mom (", "C++ Meetup", "3.5 Club" — untouched, and an
    # unescaped "(" or "+" in an alternation is a PatternError, not a place that never
    # matches. re.escape makes every name literal before it joins the alternation.
    alts = sorted({re.escape(n) for n in escaped_names if n}, key=len, reverse=True)
    return re.compile(r"(?<![\w&#;-])(" + "|".join(alts) + r")" + _WORD_TAIL, re.IGNORECASE)


def places(text: str | None, icon_map=None):
    """The text, escaped, with a glyph before any whole place name that has one.

    Home and Work default to their house/briefcase glyph with no map at all (so every
    existing caller of `places(text)` keeps working). Any other place shows a glyph only
    when `icon_map` — a {lower(name): stored icon} dict, or a callable returning one, as
    the registered filter passes the app's cached lookup — says so, and a stored "none"
    suppresses even Home and Work's default.
    """
    from markupsafe import Markup, escape

    if not text:
        return Markup("")
    imap = (icon_map() if callable(icon_map) else icon_map) or {}
    escaped = str(escape(text))
    names = {str(escape(n)) for n, v in imap.items() if v is not None} | {"home", "work"}
    pattern = _place_pattern(names)

    def sub(m):
        word = m.group(1)
        icon = _resolve_icon(word, imap.get(word.lower()))
        glyph = _place_svg(icon)
        return f'<span class="pl">{glyph}{word}</span>' if glyph else word

    return Markup(pattern.sub(sub, escaped))


def place_refs(text: str | None, refs, icon_map=None, inlink: bool = False):
    """The text as `places` draws it, with each ref's name in it (placespage.ref: name, lat,
    lon, new) a tappable place that opens the naming card (static/placecard.js). The names
    are found in order, each after the one before ("Home → Home" is two). A button; inside a
    link (`inlink`, a list row) a span that acts as one, since a button cannot sit in a
    link, which the card's script keeps from following the link. A place that still wants a
    name (`new`) is underlined; a named one shows only on hover or focus."""
    from markupsafe import Markup, escape

    if not text:
        return Markup("")
    out, at = [], 0
    for r in refs or []:
        i = text.find(r["name"], at) if r and r.get("name") else -1
        if i < 0:
            continue
        tag, kind = ("span", ' role="button" tabindex="0"') if inlink else ("button", ' type="button"')
        cls = "pn pn-new" if r.get("new") else "pn"
        out.append(places(text[at:i], icon_map))
        out.append(Markup(f'<{tag} class="{cls}"{kind} aria-haspopup="dialog" data-lat="{escape(r["lat"])}" data-lon="{escape(r["lon"])}">{places(r["name"], icon_map)}</{tag}>'))
        at = i + len(r["name"])
    out.append(places(text[at:], icon_map))
    return Markup("").join(out)


@pass_context
def _place_refs_global(context, text, refs=None, inlink=False):
    return place_refs(text, refs, context.get("place_icon_map"), inlink)


def short_place(name: str | None, keep: int = 26) -> str:
    """A place name that fits one line of a compact row: an auto-named street keeps its road
    and drops the ", City" tail when the whole would not fit. A named place is never touched."""
    if not name:
        return ""
    if len(name) <= keep or ", " not in name:
        return name
    return name.rsplit(", ", 1)[0]


def place_road(name: str | None) -> str:
    """The part of a place name before its city: "Maple Street" of "Maple Street, Shelbyville"."""
    return (name or "").rsplit(", ", 1)[0] if name and ", " in name else (name or "")


def place_city(name: str | None) -> str:
    """The city tail, comma included, or nothing: shown on a wide screen, hidden on a phone."""
    return ", " + name.rsplit(", ", 1)[1] if name and ", " in name else ""


def short_address(text: str | None) -> str:
    """A Nominatim display string ("Evergreen Terrace, Springfield, Sangamon County,
    Illinois, 62701, United States") cut to "Road, City" for a one-line card: drop a
    leading bare house number, keep the first named part as the road, then the first part
    after it that isn't a county, a postcode (all digits), or among the last two parts
    (state, country, the usual tail). No match in that window falls back to the first two
    parts as given, so a short address ("Elm Street, Springfield") passes through
    unchanged."""
    if not text:
        return ""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return ""
    if parts[0].isdigit():
        parts = parts[1:]
    if not parts:
        return ""
    road = parts[0]
    rest = parts[1:]
    candidates = rest[: len(rest) - min(2, len(rest))]
    for p in candidates:
        if p.endswith("County") or p.isdigit():
            continue
        return f"{road}, {p}"
    if len(parts) >= 2:
        return f"{parts[0]}, {parts[1]}"
    return road

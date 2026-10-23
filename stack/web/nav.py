"""Where every page sits, and links that keep your place.

The shell (base.html) draws the sidebar, the phone tab bar and the car switcher from this
tree, and the render tests read the same tree, so a page added here appears everywhere at
once. Switching car keeps the page you are on with its date, month and filters, except on
a page that is one car's record (a drive, a charge), where it goes to that list instead.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode

from settingsform import SECTIONS as _SETTING_SECTIONS

SECTIONS: tuple[tuple[str, str, str, tuple[tuple[str, str, str], ...]], ...] = (
    ("now", "Now", "/", (("now", "Now", "/"),)),
    ("timeline", "Timeline", "/timeline", (("timeline", "Feed", "/timeline"), ("drives", "Drives", "/drives"), ("charges", "Charges", "/charges"), ("tags", "Tags", "/tags"))),
    ("calendar", "Calendar", "/calendar", (("calendar", "Calendar", "/calendar"),)),
    (
        "stats",
        "Stats",
        "/stats",
        (
            ("stats", "Overview", "/stats"),
            ("charging", "Charging", "/charging"),
            ("battery", "Battery", "/battery"),
            ("sleep", "Sleep", "/sleep"),
            ("software", "Software", "/software"),
            ("service", "Service", "/service"),
        ),
    ),
    ("map", "Map", "/map", (("map", "Map", "/map"),)),
    ("places", "Places", "/places", (("places", "Places", "/places"),)),
)
# Settings sits at the sidebar's foot; its pages are its sections, each at its own URL.
SETTINGS = ("settings", "Settings", "/settings", tuple((f"settings-{k}", label, f"/settings/{k}") for k, label in _SETTING_SECTIONS))


def settings_without(hidden) -> tuple:
    """SETTINGS with the sections in `hidden` left out of its pages."""
    return (*SETTINGS[:3], tuple(p for p in SETTINGS[3] if p[0].removeprefix("settings-") not in hidden))


PHONE_TABS = ("now", "timeline", "stats", "map")

_OWNER = {page[0]: key for key, _label, _path, pages in SECTIONS + (SETTINGS,) for page in pages}
_RECORD = re.compile(r"^/(drives|charges)/\d+$")
_DROP = ("vin", "said")  # the car itself, and a one-off "saved" notice


def section_of(active: str | None) -> str:
    if active == SETTINGS[0]:
        return SETTINGS[0]
    return _OWNER.get(active or "", "now")


def car_url(here: str, vin: str) -> str:
    path, _sep, query = (here or "/").partition("?")
    path = path or "/"
    record = _RECORD.match(path)
    if record:
        return f"/{record.group(1)}?{urlencode({'vin': vin})}"
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=False) if k not in _DROP]
    return f"{path}?{urlencode(kept + [('vin', vin)])}"

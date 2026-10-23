"""What each car is: its model year, make, model, trim and paint, the drawing that stands for
it and the colour its lines take on charts and maps.

The year is read from the VIN, here, with nothing sent anywhere. Make and model come from
what the car's source reported (the vehicles table, the connector); trim and paint from
Tesla's vehicle_config, kept in cc_car_identity and read at most once a day, only while the
car is already awake. The owner can set the paint, the chart colour and the drawing by hand.

Two colours per car, kept apart on purpose: the paint is cosmetic (the drawing, the badge),
the chart colour is a line on a chart or a map and has to read on both themes, so it is
picked from PALETTE, never typed in. Pure, apart from the DDL, load/save and the worker."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import fleet_usage

LOG = logging.getLogger("carcommand.identity")

DDL = """
CREATE TABLE IF NOT EXISTS cc_car_identity (
    vin        VARCHAR PRIMARY KEY,
    paint      TEXT,
    chart      SMALLINT,
    body       TEXT,
    detected   JSONB NOT NULL DEFAULT '{}'::jsonb,
    checked_at TIMESTAMPTZ
);
-- The colour a car was first given when it had no choice of its own: kept, so adding another car never recolours it.
ALTER TABLE cc_car_identity ADD COLUMN IF NOT EXISTS chart_auto SMALLINT;
"""

# ---- the model year, from the VIN ------------------------------------------------------
# Position 10, on a 30-year cycle: A is 1980 and 2010, Y 2000 and 2030, 1 2001 and 2031.
YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"
NORTH_AMERICA = "12345"  # a VIN's first character for a car built for North America


def model_year(vin: str | None, seen: date | None = None) -> int | None:
    """The model year a VIN says, or None when it is not a valid 17-character VIN.

    A code means one of 1980+k, 2010+k, 2040+k...; a car is never more than a year ahead
    of when it was first seen, so later candidates are out. For North American VINs
    position 7 settles 1980-2009 (a digit) against 2010-2039 (a letter), and a VIN whose
    band holds no plausible year has none; elsewhere, and from 2040 when that rule's bands
    have passed, the newest plausible candidate wins."""
    v = (vin or "").strip().upper()
    if len(v) != 17 or not v.isalnum() or set(v) & set("IOQ"):
        return None
    code = v[9]
    if code not in YEAR_CODES:
        return None
    base = 1980 + YEAR_CODES.index(code)
    limit = (seen or date.today()).year + 1
    candidates = [base + 30 * k for k in range(4) if base + 30 * k <= limit]
    if not candidates:
        return None
    if v[0] in NORTH_AMERICA and candidates[-1] < 2040:
        band = range(2010, 2040) if v[6].isalpha() else range(1980, 2010)
        ruled = [y for y in candidates if y in band]
        return ruled[-1] if ruled else None
    return candidates[-1]


# ---- make and model ----------------------------------------------------------------------
WMI_MAKE = {"5YJ": "Tesla", "7SA": "Tesla", "7G2": "Tesla", "LRW": "Tesla", "XP7": "Tesla", "SFZ": "Tesla", "50E": "Lucid"}
TESLA_VIN_MODEL = {"S": "Model S", "3": "Model 3", "X": "Model X", "Y": "Model Y", "C": "Cybertruck"}
# vehicle_config's car_type, and the stream's CarType with its prefix gone, lower-cased
CAR_TYPES = {
    "model3": "Model 3",
    "modely": "Model Y",
    "models": "Model S",
    "models2": "Model S",
    "lychee": "Model S",
    "modelx": "Model X",
    "tamarind": "Model X",
    "cybertruck": "Cybertruck",
}
BASE_MODELS = ("Model 3", "Model Y", "Model S", "Model X", "Cybertruck", "Air", "Gravity")


def make_of(vin: str | None, reported: str | None) -> str | None:
    if (reported or "").strip():
        return reported.strip()
    return WMI_MAKE.get((vin or "")[:3].upper())


def split_model(model: str | None) -> tuple[str | None, str | None]:
    """'Model 3 Long Range' (as TeslaMate's names are kept) into the model and its trim."""
    m = (model or "").strip()
    for base in BASE_MODELS:
        if m.lower() == base.lower():
            return base, None
        if m.lower().startswith(base.lower() + " "):
            return base, m[len(base) :].strip() or None
    return (m or None), None


def model_of(vin: str | None, make: str | None, reported: str | None, car_type: str | None) -> tuple[str | None, str | None]:
    """(model, trim it carried) from what the source said, else Tesla's car_type, else the VIN."""
    model, trim = split_model(CAR_TYPES.get(_norm(reported), reported))
    if model:
        return model, trim
    typed = CAR_TYPES.get(_norm(car_type))
    if typed:
        return typed, None
    if (make or "").lower() == "tesla" and vin and len(vin) >= 4:
        return TESLA_VIN_MODEL.get(vin[3].upper()), None
    return None, None


def trim_name(model: str | None, badge: str | None) -> str | None:
    """Tesla's trim_badging ('74d', 'p74d', 'plaid', 'awd') in the words on the order page."""
    b = _norm(badge)
    if not b:
        return None
    if "plaid" in b:
        return "Plaid"
    if model == "Cybertruck":
        return {"awd": "All-Wheel Drive", "rwd": "Rear-Wheel Drive", "cyberbeast": "Cyberbeast"}.get(b, badge.strip().upper())
    if model in ("Model 3", "Model Y"):
        if b.startswith("p") and b[1:2].isdigit():
            return "Performance"
        if b in ("74d", "74", "75d", "78d", "82d", "lr", "longrange") or b.startswith("long"):
            return "Long Range"
        if b in ("50", "57", "58", "62", "sr", "standardrange", "standard") or b.startswith("standard"):
            return "Standard Range"
    return badge.strip().upper()


# ---- paint -------------------------------------------------------------------------------
# key -> (name, hex, make or None for any). The hex is the paint as a drawing shows it, not a
# colour match; names are the short ones the cars are sold under.
PAINTS: dict[str, tuple[str, str, str | None]] = {
    "pearlwhite": ("Pearl White", "#EDEDEA", "Tesla"),
    "solidblack": ("Solid Black", "#121215", "Tesla"),
    "diamondblack": ("Diamond Black", "#17171B", "Tesla"),
    "obsidianblack": ("Obsidian Black", "#1E1D23", "Tesla"),
    "midnightsilver": ("Midnight Silver", "#5D6168", "Tesla"),
    "silvermetallic": ("Silver", "#B9BCC1", "Tesla"),
    "quicksilver": ("Quicksilver", "#A7ABB0", "Tesla"),
    "stealthgrey": ("Stealth Grey", "#45484E", "Tesla"),
    "deepblue": ("Deep Blue", "#23417A", "Tesla"),
    "marineblue": ("Marine Blue", "#2A4A73", "Tesla"),
    "glacierblue": ("Glacier Blue", "#8EA8C0", "Tesla"),
    "redmulticoat": ("Red Multi-Coat", "#A11722", "Tesla"),
    "ultrared": ("Ultra Red", "#B3131F", "Tesla"),
    "midnightcherryred": ("Midnight Cherry Red", "#5B1220", "Tesla"),
    "stainless": ("Stainless Steel", "#B4B6B3", "Tesla"),
    "stellarwhite": ("Stellar White", "#ECEBE7", "Lucid"),
    "infiniteblack": ("Infinite Black", "#141417", "Lucid"),
    "cosmossilver": ("Cosmos Silver", "#B7BABF", "Lucid"),
    "quantumgrey": ("Quantum Grey", "#5C5F64", "Lucid"),
    "lunartitanium": ("Lunar Titanium", "#8B8880", "Lucid"),
    "eurekagold": ("Eureka Gold", "#A98A5B", "Lucid"),
    "zenithred": ("Zenith Red", "#7B1E25", "Lucid"),
    "fathomblue": ("Fathom Blue", "#233652", "Lucid"),
    "sapphireblue": ("Sapphire Blue", "#1F3C8F", "Lucid"),
    "auroragreen": ("Aurora Green", "#2E4A3D", "Lucid"),
    "supernovabronze": ("Supernova Bronze", "#8A6B4C", "Lucid"),
    "white": ("White", "#F1F1EE", None),
    "black": ("Black", "#131316", None),
    "silver": ("Silver", "#BDBFC3", None),
    "grey": ("Grey", "#6B6E74", None),
    "red": ("Red", "#A8202A", None),
    "blue": ("Blue", "#284B8C", None),
    "green": ("Green", "#2F5D46", None),
    "brown": ("Brown", "#6B4A35", None),
    "beige": ("Beige", "#CDBFA5", None),
    "yellow": ("Yellow", "#E2B93B", None),
    "orange": ("Orange", "#D8692A", None),
}
# The spellings Tesla's vehicle_config and the stream's ExteriorColor use, normalized
PAINT_ALIASES = {
    "pearl": "pearlwhite",
    "pearlwhitemulticoat": "pearlwhite",
    "midnightsilvermetallic": "midnightsilver",
    "silver": "silvermetallic",
    "metallicsilver": "silvermetallic",
    "deepbluemetallic": "deepblue",
    "multicoatred": "redmulticoat",
    "obsidianblackmetallic": "obsidianblack",
    "stealthgray": "stealthgrey",
    "stainlesssteel": "stainless",
    "gray": "grey",
}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def paint_of(code: str | None, make: str | None = None) -> dict | None:
    """A paint as {key, name, hex}: a key or Tesla's code, or a custom '#rrggbb' (no name)."""
    if not code:
        return None
    if HEX.match(code.strip()):
        return {"key": code.strip().upper(), "name": None, "hex": code.strip().upper()}
    n = _norm(code)
    # Tesla's codes first for a Tesla, so its "Silver" is Tesla's silver, not the generic one
    if (make or "").lower() == "tesla" and n in PAINT_ALIASES:
        key = PAINT_ALIASES[n]
    else:
        key = n if n in PAINTS else PAINT_ALIASES.get(n)
    if not key:
        return None
    name, hx, _ = PAINTS[key]
    return {"key": key, "name": name, "hex": hx}


def paints_for(make: str | None) -> list[dict]:
    """The swatches offered for a car: its maker's paints, or the generic ones."""
    m = (make or "").strip().lower()
    own = [k for k, p in PAINTS.items() if (p[2] or "").lower() == m]
    keys = own or [k for k, p in PAINTS.items() if p[2] is None]
    return [{"key": k, "name": PAINTS[k][0], "hex": PAINTS[k][1]} for k in keys]


# ---- the drawing -------------------------------------------------------------------------
BODIES = {
    "model3": "Model 3",
    "modely": "Model Y",
    "models": "Model S",
    "modelx": "Model X",
    "cybertruck": "Cybertruck",
    "lucidair": "Lucid Air",
    "lucidgravity": "Lucid Gravity",
    "sedan": "Sedan",
    "suv": "SUV",
    "hatchback": "Hatchback",
    "pickup": "Pickup",
    "van": "Van",
}
_BY_MODEL = {"Model 3": "model3", "Model Y": "modely", "Model S": "models", "Model X": "modelx", "Cybertruck": "cybertruck"}
_KEYWORDS = (
    ("pickup", ("truck", "pickup", "f-150", "lightning", "silverado", "sierra", "r1t", "hummer", "ram")),
    ("van", ("van", "transit", "buzz", "sienna", "odyssey", "pacifica", "sprinter")),
    (
        "suv",
        (
            "suv",
            "x1",
            "x3",
            "x5",
            "ix",
            "ex30",
            "ex90",
            "xc40",
            "xc60",
            "xc90",
            "c40",
            "q4",
            "q8",
            "id.4",
            "id4",
            "ioniq 5",
            "mach-e",
            "r1s",
            "r2",
            "ev9",
            "ev6",
            "lyriq",
            "equinox",
            "blazer",
            "enyaq",
            "kona",
        ),
    ),
    ("hatchback", ("hatch", "golf", "id.3", "id3", "bolt", "leaf", "i3", "mini", "zoe", "500e", "ioniq 6")),
)


def body_for(make: str | None, model: str | None) -> str:
    """The drawing a model gets unless the owner picks another."""
    if model in _BY_MODEL:
        return _BY_MODEL[model]
    if (make or "").lower() == "lucid":
        return "lucidgravity" if "gravity" in (model or "").lower() else "lucidair"
    m = f" {(model or '').lower()} "
    for body, words in _KEYWORDS:
        if any(w in m for w in words):
            return body
    return "sedan"


# ---- chart colours -----------------------------------------------------------------------
# (name, light theme, dark theme). The first six are the car colours the site always had, in
# its order; gold and teal are a shade darker on the light theme to read against it. Never
# the blue of driving or the green of charging. style.css and maps.js carry these same values
# (a test holds them to it).
PALETTE = (
    ("Gold", "#A87F12", "#D9A82A"),
    ("Violet", "#8A5FD6", "#8A5FD6"),
    ("Coral", "#D6605C", "#D6605C"),
    ("Teal", "#2F8F8F", "#3FA8A8"),
    ("Orchid", "#B06AA0", "#B06AA0"),
    ("Olive", "#8C7A3E", "#8C7A3E"),
    ("Rust", "#C2410C", "#C2410C"),
    ("Raspberry", "#D9467A", "#D9467A"),
    ("Moss", "#6B8526", "#7F9C2E"),
    ("Slate", "#56708F", "#7D93B3"),
    # a match for the common paints: blue and green kept off driving's and charging's, a light
    # neutral for white and silver, a dark one for black (a mid grey on the dark theme)
    ("Blue", "#2A4E90", "#4866A8"),
    ("Green", "#306048", "#42725A"),
    ("Steel", "#8A8A7E", "#C3CAD1"),
    ("Charcoal", "#41464D", "#909088"),
)
DEFAULTS = 6  # the old palette: cars past the sixth went round it again
SURFACES = {"light": ("#F6F5F0", "#FFFFFF"), "dark": ("#141319", "#1D1C24")}  # page and panel
STATES = {"light": ("#2F6FE0", "#2E9E5B"), "dark": ("#5E8FF0", "#3FBF73")}  # driving, charging
# The site's own greys (idle, asleep, muted text, ink, rules): a car's line is never one of them
UI_GREYS = {"light": ("#8E8AA0", "#C9C5D4", "#6B6875", "#1B1A21", "#E4E1D8"), "dark": ("#6B6880", "#3A3846", "#A09CAB", "#F0EDE4", "#2C2A35")}
MIN_FROM_UI = 12.0
# The colours a paint picks from: the clear ones, not Gold (the battery's and the mark's) and not
# the muted paint matches, which sat dark and close together beside each other. Those stay for
# picking by hand. A white, grey or black paint takes Slate.
AUTO = tuple(range(1, 10))
NEUTRAL = 9
GREY_PAINT = 10.0  # a paint with less colour than this (CIELAB chroma) is white, grey or black
MIN_CONTRAST = 3.0  # a line or a dot against the page: WCAG's floor for graphics
MIN_APART = 18.0  # CIE76 distance two colours on one chart keep between them
MIN_DISTINCT = 20.0  # CIEDE2000 distance two cars' colours keep, on each theme: blue beside charcoal was 16


def _rgb(hx: str) -> tuple[float, float, float]:
    h = hx.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hx: str) -> float:
    r, g, b = (_lin(c) for c in _rgb(hx))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def lab(hx: str) -> tuple[float, float, float]:
    r, g, b = (_lin(c) for c in _rgb(hx))
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def apart(a: str, b: str) -> float:
    return sum((p - q) ** 2 for p, q in zip(lab(a), lab(b))) ** 0.5


def de2000(a: str, b: str) -> float:
    """CIEDE2000: how different two colours look to a person, which plain Lab distance (apart)
    overstates for dark blues and greys. Sharma, Wu and Dalal's formulation."""
    (l1, a1, b1), (l2, a2, b2) = lab(a), lab(b)
    cbar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dh = 0.0 if c1p * c2p == 0 else h2p - h1p if abs(h2p - h1p) <= 180 else h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    d_l, d_c, d_h = l2 - l1, c2p - c1p, 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    lbar, cbarp = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbar = (h1p + h2p) / 2
    else:
        hbar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = 1 - 0.17 * math.cos(math.radians(hbar - 30)) + 0.24 * math.cos(math.radians(2 * hbar)) + 0.32 * math.cos(math.radians(3 * hbar + 6)) - 0.2 * math.cos(math.radians(4 * hbar - 63))
    rt = -math.sin(math.radians(60 * math.exp(-(((hbar - 275) / 25) ** 2)))) * 2 * math.sqrt(cbarp**7 / (cbarp**7 + 25**7))
    sl = 1 + 0.015 * (lbar - 50) ** 2 / math.sqrt(20 + (lbar - 50) ** 2)
    sc, sh = 1 + 0.045 * cbarp, 1 + 0.015 * cbarp * t
    return math.sqrt((d_l / sl) ** 2 + (d_c / sc) ** 2 + (d_h / sh) ** 2 + rt * (d_c / sc) * (d_h / sh))


def _distinct(i: int, j: int) -> float:
    """How far apart two chart colours look: the nearer of the two themes."""
    return min(de2000(PALETTE[i][1], PALETTE[j][1]), de2000(PALETTE[i][2], PALETTE[j][2]))


# Each colour's neighbours too close to share a chart with it, worked out once.
TOO_CLOSE: dict[int, frozenset[int]] = {i: frozenset(j for j in range(len(PALETTE)) if j != i and _distinct(i, j) < MIN_DISTINCT) for i in range(len(PALETTE))}


def blocked(taken) -> set[int]:
    """The chart colours another car's colours rule out: theirs, and any too close to one."""
    out = set(taken)  # a generator is read once, here
    for t in list(out):
        out |= TOO_CLOSE.get(t, frozenset())
    return out


def too_close(index: int, colors: dict[str, int], vin: str) -> str | None:
    """The other car whose colour this one would sit too close to (or on), if any."""
    return next((v for v, c in colors.items() if v != vin and (c == index or c in TOO_CLOSE.get(index, ()))), None)


def valid_chart(i) -> bool:
    return isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(PALETTE)


def chart_colors(vins: list[str], chosen: dict[str, int | None], paints: dict[str, str | None] | None = None, kept: dict[str, int | None] | None = None) -> dict[str, int]:
    """vin -> PALETTE index, for every page. No two cars' colours sit too close (MIN_DISTINCT)
    while the palette has room. A car's own choice comes first, in the garage's order (a later
    car whose choice sits on or beside an earlier one's is picked for as if it had none). A car
    with no choice takes the free colour nearest its paint, as the owner set it or the car
    reports it; with no paint known, the colour its place in the garage gives it (from Violet on,
    never Gold), else the first one free. `kept` is what such a car was given before: it keeps that
    (after the hand-picked ones) while it is free, so a car joining the garage never recolours one
    already drawn; only a car without one is picked for, around those."""
    out: dict[str, int] = {}
    paints = paints or {}
    kept = kept or {}
    for vin in vins:
        c = chosen.get(vin)
        if valid_chart(c) and c not in blocked(out.values()):
            out[vin] = c
    for vin in vins:
        c = kept.get(vin)
        if vin not in out and valid_chart(c) and c not in blocked(out.values()):
            out[vin] = c
    for i, vin in enumerate(vins):
        if vin in out:
            continue
        ruled_out = blocked(out.values())
        free = [k for k in range(len(PALETTE)) if k not in ruled_out]
        pick = nearest_chart(paints.get(vin), ruled_out) if free else None
        if pick is None:
            want = AUTO[i % DEFAULTS]  # never Gold: that is the battery's and the mark's
            if want in free:
                pick = want
            elif free:
                pick = next((k for k in free if k in AUTO), next((k for k in free if k), free[0]))  # Gold last of all
            else:  # more cars than distinct colours: never share one, at least
                spare = [k for k in range(len(PALETTE)) if k not in out.values()]
                pick = spare[0] if spare else i % len(PALETTE)
        out[vin] = pick
    return out


def chart_taken(vin: str, index: int, colors: dict[str, int]) -> str | None:
    """The car already drawn in this colour, if another one is."""
    return next((v for v, c in colors.items() if c == index and v != vin), None)


def nearest_chart(paint_hex: str | None, avoid: set[int] = frozenset()) -> int | None:
    """The free colour of AUTO nearest a paint's hue, lightness counting half (a chart colour
    cannot be as light or dark as paint is); a white, silver, grey or black paint takes Slate.
    None for no paint, a neutral paint with Slate taken, or no free colour: the car then takes
    its place in the garage's order."""
    if not paint_hex or not HEX.match(paint_hex):
        return None
    pl, pa, pb = lab(paint_hex)
    options = [i for i in AUTO if i not in avoid]

    def hue_gap(i):
        cl, ca, cb = lab(PALETTE[i][1])
        return ((0.5 * (pl - cl)) ** 2 + (pa - ca) ** 2 + (pb - cb) ** 2) ** 0.5

    if (pa * pa + pb * pb) ** 0.5 < GREY_PAINT:
        return NEUTRAL if NEUTRAL in options else None
    return min(options, key=hue_gap, default=None)


def chart_after_look(chart: int | None, current: int, touched: bool, paint_before: str | None, paint_after: dict | None, avoid: set[int]) -> int | None:
    """The chart colour a Look save keeps: one picked by hand wins; otherwise a new paint takes
    its matching free colour, and an unchanged paint leaves the colour as it was."""
    if touched or (chart is not None and chart != current):
        return chart
    if (paint_after or {}).get("key") == paint_before or not paint_after:
        return chart
    near = nearest_chart(paint_after.get("hex"), avoid)
    return near if near is not None else chart


# ---- what a car is, in one line -------------------------------------------------------------
def describe(vin: str, make: str | None, model: str | None, row: dict | None, seen: date | None = None) -> dict:
    """Everything a page shows of a car's identity. `row` is its cc_car_identity row, if any."""
    row = row or {}
    det = row.get("detected") or {}
    make = make_of(vin, make)
    model, carried_trim = model_of(vin, make, model, det.get("car_type"))
    trim = trim_name(model, det.get("trim_badging")) or carried_trim
    detected_paint = paint_of(det.get("exterior_color"), make)
    paint = paint_of(row.get("paint"), make) or detected_paint
    year = model_year(vin, seen)
    title = " ".join(str(x) for x in (year, make, model, trim) if x)
    paint_name = (paint or {}).get("name")
    return {
        "vin": vin,
        "year": year,
        "make": make,
        "model": model,
        "trim": trim,
        "title": title,
        "line": f"{title} · {paint_name}" if title and paint_name else title or paint_name or "",
        # The Now cards' one line: no trim, which is what pushed it onto a second line.
        "short": " · ".join(x for x in (" ".join(str(x) for x in (year, make, model) if x), paint_name) if x),
        "paint": paint,
        "paint_set": bool(row.get("paint")),
        "detected_paint": detected_paint,
        "body": row.get("body") if row.get("body") in BODIES else body_for(make, model),
        "body_set": row.get("body") in BODIES,
        # True, False or None (not said): what the car's maker says it is fitted with
        "fitted": {ours: det.get(ours) for ours in FITTED.values()},
    }


# ---- Tesla's answer ----------------------------------------------------------------------
TESLA_KEYS = ("car_type", "trim_badging", "exterior_color", "wheel_type", "roof_color", "perf_config")
# The stream's names for the same facts (fleet-telemetry protos/vehicle_data.proto)
STREAM_KEYS = {"CarType": "car_type", "Trim": "trim_badging", "ExteriorColor": "exterior_color", "WheelType": "wheel_type", "RoofColor": "roof_color"}


# What the car is fitted with, for the controls it is offered: vehicle_config's name -> ours
FITTED = {"has_seat_cooling": "seat_cooling", "rear_seat_heaters": "rear_seat_heaters"}


def tesla_config(body: dict | None) -> dict:
    """The few identity fields of a vehicle_data answer, what it is fitted with, and nothing else of it."""
    r = (body or {}).get("response") or {}
    out = {}
    for k, ours in FITTED.items():
        v = (r.get("vehicle_config") or {}).get(k)
        if isinstance(v, bool):
            out[ours] = v
        elif isinstance(v, int):
            out[ours] = v > 0
    for part in ("vehicle_config", "vehicle_state"):
        got = r.get(part) or {}
        for k in TESLA_KEYS:
            v = got.get(k)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip() and k not in out:
                out[k] = str(v).strip()
    return out


def from_stream(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in STREAM_KEYS and isinstance(v, str) and v.strip():
            out[STREAM_KEYS[k]] = v.strip()
    return out


# ---- storage -----------------------------------------------------------------------------
def load(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT vin, paint, chart, chart_auto, body, detected, checked_at FROM cc_car_identity").fetchall()
    out = {}
    for r in rows:
        r = dict(r)
        if isinstance(r.get("detected"), str):
            r["detected"] = json.loads(r["detected"])
        out[r["vin"]] = r
    return out


def save_choice(conn, vin: str, paint: str | None, chart: int | None, body: str | None) -> None:
    """The owner's look for a car. Editing it forgets the colour kept for it, so a new paint is matched afresh."""
    conn.execute(
        "INSERT INTO cc_car_identity (vin, paint, chart, body) VALUES (%s, %s, %s, %s) ON CONFLICT (vin) DO UPDATE SET paint = EXCLUDED.paint, chart = EXCLUDED.chart, body = EXCLUDED.body, chart_auto = NULL",
        (vin, paint, chart, body),
    )


def keep_colors(conn, picked: dict[str, int]) -> None:
    """Remember the colours first given to cars with none kept (only ever fills a blank)."""
    for vin, index in picked.items():
        conn.execute(
            "INSERT INTO cc_car_identity (vin, chart_auto) VALUES (%s, %s) ON CONFLICT (vin) DO UPDATE SET chart_auto = COALESCE(cc_car_identity.chart_auto, EXCLUDED.chart_auto)",
            (vin, index),
        )


def save_detected(conn, vin: str, found: dict, checked_at: datetime | None = None) -> None:
    """Merge what a source found into the car's row; `checked_at` when Tesla was asked."""
    conn.execute(
        "INSERT INTO cc_car_identity (vin, detected, checked_at) VALUES (%s, %s::jsonb, %s) "
        "ON CONFLICT (vin) DO UPDATE SET detected = cc_car_identity.detected || EXCLUDED.detected, checked_at = COALESCE(EXCLUDED.checked_at, cc_car_identity.checked_at)",
        (vin, json.dumps(found), checked_at),
    )


def parse_choice(form, vin: str, make: str | None) -> tuple[str | None, int | None, str | None]:
    """(paint, chart, body) from Settings › Cars and sources: blank or 'auto' is the car's own.
    A colour picker moved from where the page set it (paint_hex0) is a custom paint, whichever
    swatch is ticked, so picking one needs no script to tick 'custom' too."""
    p = (form.get(f"paint_{vin}") or "").strip()
    hx = (form.get(f"paint_hex_{vin}") or "").strip().upper()
    moved = HEX.match(hx) and hx != (form.get(f"paint_hex0_{vin}") or "").strip().upper()
    if moved or p == "custom":
        p = hx if HEX.match(hx) else ""
    paint = (paint_of(p, make) or {}).get("key") if p and p != "auto" else None
    c = (form.get(f"chart_{vin}") or "").strip()
    chart = int(c) if c.isascii() and c.isdecimal() and valid_chart(int(c)) else None
    b = (form.get(f"body_{vin}") or "").strip()
    return paint, chart, b if b in BODIES else None


# ---- the worker: Tesla's config, once a day, only while the car is awake ----------------------
DAY = timedelta(hours=24)
LIST_GAP = timedelta(hours=3)  # asking which cars are awake, at most this often while one is due
TRIES_A_DAY = 2  # a car whose read failed on Tesla's side (5xx, no answer) is tried this often a day


GUARD_NAMES = {"tesla": "Sentry", "lucid": "Halo Secure"}


def guard_name(make: str | None) -> str:
    """What the car's guard mode is called: Sentry on a Tesla, Halo Secure on a Lucid."""
    return GUARD_NAMES.get((make or "").strip().lower(), "Guard mode")


def guard_label(makes) -> str:
    """One name for a set of cars: each make's own, joined ("Sentry / Halo Secure") when they differ."""
    names = list(dict.fromkeys(guard_name(m) for m in makes))
    return " / ".join(names) if names else guard_name(None)


def is_tesla(vin: str) -> bool:
    return WMI_MAKE.get((vin or "")[:3].upper()) == "Tesla"


class Worker(threading.Thread):
    """Keeps cc_car_identity's detected fields. Every hour: what the stream carried, then, for a
    Tesla car in the garage not read in a day, vehicle_config — asked only of a car Tesla's list
    says is online. vehicle_data never wakes a car (an asleep one answers 408), and it is never
    asked of one that is not already awake. `vins()` is the garage; `after()` runs when anything
    changed (the page cache).

    Tesla's answers decide when to ask again: a refusal (4xx but 408) is kept as the day's read,
    so that car waits a day; 429 ends the round; a 5xx or no answer is tried again next round,
    at most TRIES_A_DAY times a day."""

    def __init__(
        self,
        connect: Callable,
        access: Callable,
        fleet: Callable,
        streamed: Callable,
        after: Callable,
        vins: Callable,
        interval_s: int = 3600,
    ) -> None:
        super().__init__(daemon=True, name="car-identity")
        self.connect, self.access, self.fleet, self.streamed, self.after, self.vins = (
            connect,
            access,
            fleet,
            streamed,
            after,
            vins,
        )
        self.interval_s = interval_s
        self.listed_at: datetime | None = None
        self.failed: dict[str, list[datetime]] = {}  # vin -> when Tesla's side failed, the last day
        self.stop = threading.Event()

    def round(self, now: datetime | None = None) -> list[str]:
        """One pass; the cars whose identity changed."""
        now = now or datetime.now(timezone.utc)
        changed = []
        with self.connect() as conn:
            rows = load(conn)
            for vin, fields in (self.streamed() or {}).items():
                found = from_stream(fields)
                if found and any((rows.get(vin) or {}).get("detected", {}).get(k) != v for k, v in found.items()):
                    save_detected(conn, vin, found)
                    changed.append(vin)
            conn.commit()
        changed += self.ask_tesla(rows, now)
        if changed:
            self.after()
        return changed

    def due(self, rows: dict, now: datetime) -> list[str]:
        """The garage's Teslas not read in a day and not out of tries."""
        out = []
        for vin in self.vins() or []:
            if not is_tesla(vin):
                continue
            last = (rows.get(vin) or {}).get("checked_at")
            if last and now - last < DAY * fleet_usage.METER.factor():
                continue
            self.failed[vin] = [t for t in self.failed.get(vin, []) if now - t < DAY]
            if len(self.failed[vin]) >= TRIES_A_DAY:
                continue
            out.append(vin)
        return out

    def ask_tesla(self, rows: dict, now: datetime) -> list[str]:
        from tesla_fleet import TeslaError

        due = self.due(rows, now)
        if not due:
            return []  # nothing to read: the account's list is not asked for either
        fleet = self.fleet()
        if fleet is None or (self.listed_at and now - self.listed_at < timedelta(seconds=fleet_usage.slowed(LIST_GAP.total_seconds()))):
            return []
        got = self.access()
        if not got:
            return []
        token, base = got
        self.listed_at = now
        try:
            awake = [c["vin"] for c in fleet.vehicles(token, base) if c.get("vin") in due and c.get("state") == "online"]
        except TeslaError as err:
            LOG.warning("car identity: %s", err)
            return []
        changed = []
        for vin in awake:
            try:
                found = tesla_config(fleet.vehicle_config(token, base, vin))
            except TeslaError as err:
                if err.status == 408:
                    continue  # it fell asleep since the list: next time, no try spent
                if err.status == 429:
                    LOG.warning("car identity: Tesla asks to slow down; the rest wait for the next round")
                    break
                LOG.warning("car identity: %s", err)
                if err.status is not None and 400 <= err.status < 500:
                    found = {}  # refused: kept as the day's read, so it waits a day
                else:
                    self.failed.setdefault(vin, []).append(now)
                    continue
            with self.connect() as conn:
                save_detected(conn, vin, found, now)
                conn.commit()
            if found:
                changed.append(vin)
        return changed

    def run(self) -> None:
        self.stop.wait(240)  # let startup settle
        while not self.stop.is_set():
            try:
                self.round()
            except Exception as err:  # noqa: BLE001 - a bad pass waits for the next one
                LOG.warning("car identity: %s", err)
            self.stop.wait(self.interval_s)

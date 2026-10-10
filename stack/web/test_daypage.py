"""What the Day page says about each row."""

from datetime import datetime, timedelta, timezone

from daypage import day_figs, gap_text, row_parts


class U:
    """Imperial, converting nothing: what the route passes is units_mod.ACTIVE."""

    labels = {"dist": "mi", "econ": "Wh/mi"}

    def distance(self, x):
        return x

    def per_distance(self, x):
        return x


class Metric(U):
    labels = {"dist": "km", "econ": "Wh/km"}

    def distance(self, x):
        return x * 1.609344

    def per_distance(self, x):
        return x / 1.609344


T0 = datetime(2026, 9, 26, 15, 49, tzinfo=timezone.utc)
DRIVE = {
    "kind": "drive",
    "start": T0,
    "end": T0 + timedelta(minutes=9),
    "title": "Home → Elm Street",
    "miles": 3.1,
    "duration": timedelta(minutes=9),
    "wh_per_mi": 479.0,
    "cost": 0.46,
    "level_start": 59.0,
    "level_end": 57.0,
}


def test_drive_row_parts():
    p = row_parts(DRIVE, U(), "$")
    assert p["title"] == "Home → Elm Street" and p["big"] == "3.1 mi" and p["sub"] == "59→57%"
    assert p["pills"] == ["9 min", "479 Wh/mi", "$0.46"]
    m = row_parts(DRIVE, Metric(), "€")
    assert m["big"] == "5.0 km" and m["pills"] == ["9 min", "298 Wh/km", "€0.46"]


def test_charge_row_parts():
    c = {
        "kind": "charge",
        "start": T0,
        "end": T0 + timedelta(hours=2),
        "title": "Home",
        "kwh": 17.4,
        "duration": timedelta(hours=2),
        "charging_type": "DC",
        "cost": 5.2,
        "level_start": 40.0,
        "level_end": 58.0,
    }
    p = row_parts(c, U(), "$")
    assert p["big"] == "17.4 kWh" and p["pills"] == ["2 h", "fast", "$5.20"] and p["sub"] == "40→58%"
    running = dict(c, end=None, duration=None, kwh=None, cost=None, level_end=None)
    assert row_parts(running, U(), "$") == {"title": "Home", "pills": ["fast"], "big": "—", "sub": ""}


def test_gap_text_names_the_guard_mode_of_the_car():
    import identity

    parked = {"kind": "parked", "sub": "idle", "duration": timedelta(hours=3), "kwh_loss": None, "sentry_h": 1.0}
    for make, word in (("Lucid", "Halo Secure"), ("Tesla", "Sentry"), (None, "Guard mode")):
        assert gap_text(parked, None, U(), identity.guard_name(make)).endswith(f"· {word} 1 h")
    assert gap_text(dict(parked, sentry_h=3.0), None, U()).endswith("· Guard mode on")


def test_gap_text_reads_like_a_sentence():
    parked = {"kind": "parked", "sub": "sleep", "duration": timedelta(hours=2, minutes=47), "kwh_loss": 0.19, "range_loss": 0.6, "sentry_h": 0.0}
    assert gap_text(parked, "Evergreen Terrace", U()) == "Asleep 2 h 47 min at Evergreen Terrace · lost 0.2 kWh, 0.6 mi"
    assert gap_text(dict(parked, sub="idle", kwh_loss=None, range_loss=None), None, U()) == "Parked 2 h 47 min"
    assert gap_text(dict(parked, sentry_h=2.0), "Home", U(), "Sentry") == "Asleep 2 h 47 min at Home · lost 0.2 kWh, 0.6 mi · Sentry 2 h"
    assert gap_text(dict(parked, sentry_h=0.7, kwh_loss=0.01), "Home", U(), "Sentry") == "Asleep 2 h 47 min at Home · Sentry 42 min"
    assert (
        gap_text(dict(parked, sub="idle", sentry_h=2.7, kwh_loss=None), None, U(), "Sentry") == "Parked 2 h 47 min · Sentry on"
    ), "Sentry for nearly all of it reads as on, never longer than the stretch"


def test_gap_text_falls_back_to_the_battery_drop_without_a_kwh_figure():
    parked = {"kind": "parked", "sub": "sleep", "duration": timedelta(hours=8), "kwh_loss": None, "drop": 1.0, "sentry_h": 0.0}
    assert gap_text(parked, None, U()) == "Asleep 8 h · lost 1.0%"
    assert gap_text(dict(parked, drop=0.2), None, U()) == "Asleep 8 h", "a drop under half a percent is reading noise"
    assert gap_text(dict(parked, kwh_loss=0.6), None, U()) == "Asleep 8 h · lost 0.6 kWh", "kWh wins when known"


def test_day_figs():
    assert day_figs(DRIVE, U(), "$") == "3.1 mi · 59→57% · 9 min · 479 Wh/mi · $0.46"
    assert day_figs({"kind": "charge", "kwh": 17.4, "duration": timedelta(hours=2), "level_start": 40.0, "level_end": 58.0, "cost": None}, U(), "$") == "40→58% · 17.4 kWh · 2 h"


def test_detail_line_carries_the_second_tier_of_a_row():
    from daypage import detail_line
    from units import Units

    drive = {
        "kind": "drive",
        "miles": 29.4,
        "duration": timedelta(minutes=36),
        "kwh_used": 8.34,
        "range_start": 180.0,
        "range_end": 150.6,
        "max_mph": 79.4,
        "temp_f": 68.2,
        "inside_start": 71.0,
        "inside_end": 74.0,
        "climb_ft": 320.0,
        "descent_ft": 280.0,
        "fsd_miles": 12.3,
    }
    assert detail_line(drive, Units(), "$") == ["8.3 kWh", "29.4 mi rated", "avg 49 mph", "max 79 mph", "68°F out", "cabin 71→74°F", "↑320 ft ↓280 ft", "FSD 12.3 mi"]
    km = detail_line(drive, Units(metric=True), "€")
    assert "47.3 km rated" in km and "max 128 km/h" in km and "20°C out" in km and "↑98 m ↓85 m" in km
    # the Timeline's events carry rated_used and a weather word instead of the range pair
    feed = {"kind": "drive", "miles": 2.0, "duration": timedelta(minutes=5), "rated_used": 2.6, "weather": "Light rain"}
    assert detail_line(feed, Units(), "$") == ["2.6 mi rated", "avg 24 mph", "Light rain"]
    charge = {"kind": "charge", "kwh": 76.3, "duration": timedelta(hours=8, minutes=5), "range_added": 214.0, "rate_plan": "E-TOU-C", "charging_type": "AC"}
    assert detail_line(charge, Units(), "$") == ["+214 mi rated", "9.4 kW avg", "E-TOU-C"]
    assert detail_line({"kind": "charge", "kwh": None, "duration": None}, Units(), "$") == [], "nothing known, nothing said"
    assert detail_line({"kind": "drive", "miles": 0.01, "duration": timedelta(seconds=20)}, Units(), "$") == [], "no speed from a blip"


def test_gap_text_adds_the_rated_miles_and_the_cabin():
    parked = {"kind": "parked", "sub": "sleep", "duration": timedelta(hours=6), "kwh_loss": 0.2, "range_loss": 0.8, "inside_start": 68.0, "inside_end": 61.0, "sentry_h": 0.0}
    from units import Units

    assert gap_text(parked, "Home", Units()) == "Asleep 6 h at Home · lost 0.2 kWh, 0.8 mi · cabin 68→61°F"


def test_an_unchanged_cabin_is_one_figure():
    from units import Units

    parked = {"kind": "parked", "sub": "sleep", "duration": timedelta(hours=1), "inside_start": 72.2, "inside_end": 71.8}
    assert gap_text(parked, None, Units()) == "Asleep 1 h · cabin 72°F"


def test_gap_text_falls_back_to_percent_when_the_kwh_figure_rounds_away_and_skips_placeholder_places():
    parked = {"kind": "parked", "sub": "sleep", "duration": timedelta(hours=8), "kwh_loss": 0.02, "drop": 1.0, "sentry_h": 0.0}
    assert gap_text(parked, None, U()) == "Asleep 8 h · lost 1.0%", "coarse range readings, a real 1% drop"
    for placeholder in ("Charge", "?", "In progress", ""):
        assert gap_text(parked, placeholder, U()) == "Asleep 8 h · lost 1.0%", placeholder


def test_list_records_become_rows_and_group_by_local_day():
    from zoneinfo import ZoneInfo

    from daypage import as_charge, as_drive, by_day
    from units import Units

    d = {
        "id": 5,
        "start_date": T0,
        "destination_date": T0 + timedelta(minutes=9),
        "from": "Home",
        "to": "Elm Street",
        "miles": 3.1,
        "duration": timedelta(minutes=9),
        "wh_per_mi": 479.0,
        "cost": 0.46,
        "level_start": 59.0,
        "level_end": 57.0,
    }
    r = as_drive(d)
    assert r["kind"] == "drive" and r["title"] == "Home → Elm Street" and r["start"] == T0 and r["end"] == d["destination_date"]
    assert row_parts(r, Units(), "$")["big"] == "3.1 mi"
    c = {"id": 9, "session_start_date": T0, "session_end_date": None, "where": None, "kwh": 30.0, "start_level": 40.0, "end_level": 80.0, "duration": None}
    rc = as_charge(c)
    assert rc["title"] == "Charge" and rc["level_start"] == 40.0 and rc["end"] is None and row_parts(rc, Units(), "$")["sub"] == "40→80%"
    tz = ZoneInfo("America/Los_Angeles")
    late = dict(d, id=6, start_date=T0.replace(hour=6), miles=1.0)  # 06:49 UTC is 11:49 PM the evening before in Springfield's zone
    days = by_day([d, late], tz, "start_date")
    assert [len(x["rows"]) for x in days] == [1, 1] and days[0]["miles"] == 3.1 and days[0]["date"] > days[1]["date"]
    assert by_day([c], tz, "session_start_date")[0]["kwh"] == 30.0
    never = dict(c, id=10, session_start_date=None)
    days = by_day([c, never], tz, "session_start_date")
    assert [x["date"] is None for x in days] == [False, True], "a charge that never started (only the hidden list has one) groups under no date instead of failing"

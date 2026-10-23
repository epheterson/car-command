"""The Software page per version: range at 100 % while it ran and over how many charges,
against the version before, the outside temperature, how often updates come, a year as bars."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import softwarepage

TZ = ZoneInfo("America/Chicago")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def rows():
    """Three versions, newest first, as software_rows gives them."""
    return [
        {"version": "2026.8.2", "seen_at": T0 + timedelta(days=60), "until": None},
        {"version": "2026.2.8", "seen_at": T0 + timedelta(days=30), "until": T0 + timedelta(days=60)},
        {"version": "2025.48.4", "seen_at": T0, "until": T0 + timedelta(days=30)},
    ]


def test_each_version_gets_the_charges_and_drives_that_happened_while_it_ran():
    charges = [(T0 + timedelta(days=d), 1000.0 + d, full) for d, full in ((2, 300.0), (10, 302.0), (20, 304.0), (35, 298.0), (45, 296.0), (70, 299.0))]
    temps = [(T0 + timedelta(days=d), t) for d, t in ((1, 30.0), (5, 40.0), (40, 60.0), (65, 80.0), (70, 70.0))]
    new, mid, old = softwarepage.per_version(rows(), charges, temps)
    assert (old["full"], old["full_n"], old["temp_f"]) == (302.0, 3, 35.0)
    assert (mid["full"], mid["full_n"], mid["temp_f"]) == (297.0, 2, 60.0)
    assert (new["full"], new["full_n"], new["temp_f"]) == (299.0, 1, 75.0), "the running version runs to now"
    assert mid["full_change"] == -5.0 and new["full_change"] == 2.0 and old["full_change"] is None, "each against the version before it"


def test_a_version_with_no_charge_says_nothing_rather_than_zero():
    new, mid, old = softwarepage.per_version(rows(), [(T0 + timedelta(days=2), 1.0, 300.0)], [])
    assert mid["full"] is None and mid["full_n"] == 0 and mid["temp_f"] is None and new["full_change"] is None


def test_cadence_is_the_mean_gap_and_the_days_since_the_last():
    c = softwarepage.cadence(rows(), T0 + timedelta(days=70))
    assert c == {"every": 30.0, "since": 10}
    assert softwarepage.cadence([], T0) == {"every": None, "since": None}


def test_installs_in_one_sitting_are_one_update():
    chain = [{"version": f"3.1.{n}", "seen_at": T0 + timedelta(hours=h)} for n, h in ((51, 0), (55, 1), (60, 11), (61, 12))]
    later = [{"version": "3.3.20", "seen_at": T0 + timedelta(days=60)}, {"version": "3.4.1", "seen_at": T0 + timedelta(days=120)}]
    assert softwarepage.cadence(later + chain, T0 + timedelta(days=130))["every"] == 60.0, "the factory's chain is one update, not four"


def test_a_run_counts_the_local_dates_shown_and_hours_under_a_day():
    # Jun 25 23:46 to Jul 2 22:04 in Chicago: 6.9 days of hours, 7 between the dates the page shows
    a, b = datetime(2026, 6, 26, 4, 46, tzinfo=timezone.utc), datetime(2026, 7, 3, 3, 4, tzinfo=timezone.utc)
    r = softwarepage.ran(a, b, b, TZ)
    assert (r["days"], r["brief"]) == (7, None) and r["secs"] == (b - a).total_seconds()
    assert softwarepage.ran(a, a + timedelta(minutes=16), b, TZ)["brief"] == (16, "min")
    assert softwarepage.ran(a, a + timedelta(hours=10, minutes=40), b, TZ)["brief"] == (11, "h"), "under a day crossing midnight is hours, not 1 day"
    assert softwarepage.ran(a, None, a + timedelta(days=3), TZ)["days"] == 3, "the running version runs to now"


def test_the_timeline_is_a_dot_a_day_an_update_arrived():
    now = T0 + timedelta(days=70)
    tl = softwarepage.timeline(rows(), now, TZ)
    assert [d["label"].split(" · ")[0] for d in tl["dots"]] == ["2026.8.2", "2026.2.8", "2025.48.4"] and tl["n"] == 3
    assert tl["dots"][0]["current"] and not tl["dots"][2]["current"]
    assert abs(tl["dots"][0]["x"] - 100 * (365 - 10) / 365) < 0.01, "placed on its day"
    assert tl["dots"][2]["label"].endswith("Dec 31, 2025"), "midnight UTC on Jan 1 is still December in Chicago"
    assert all(t["text"][:3] in ("Jan", "Mar", "May", "Jul", "Sep", "Nov") for t in tl["ticks"]) and any(t["text"] == "Jan 2026" for t in tl["ticks"])
    assert softwarepage.timeline(rows(), T0 + timedelta(days=800), TZ)["dots"] == [], "updates older than a year are off the line"


def test_a_change_on_few_charges_or_inside_the_wobble_is_quiet():
    charges = [(T0 + timedelta(days=d), 1.0, f) for d, f in [(i, 300.0) for i in range(31, 41)] + [(i, 303.0) for i in range(61, 71)] + [(2, 300.5)]]
    new, mid, old = softwarepage.per_version(rows(), charges, [])
    assert new["full_change"] == 3.0 and not new["full_quiet"], "ten charges a side, three miles: said"
    assert round(mid["full_change"], 1) == -0.5 and mid["full_quiet"], "one charge behind the version before"


def test_the_waiting_update_needs_a_version_and_keeps_only_a_safe_link():
    car = {"update_available": True, "update_version": "3.1.2", "update_install_min": 30, "update_notes": " First.\n\n  Second. \n", "update_notes_url": "https://example.com/n"}
    assert softwarepage.waiting(car) == {"version": "3.1.2", "minutes": 30, "notes": ["First.", "Second."], "url": "https://example.com/n"}
    assert softwarepage.waiting(dict(car, update_notes_url="javascript:alert(1)"))["url"] is None
    assert softwarepage.waiting(dict(car, update_notes=None, update_notes_url=None)) == {"version": "3.1.2", "minutes": 30, "notes": [], "url": None}, "a Gravity: no notes"
    assert softwarepage.waiting(dict(car, update_version=None)) is None, "connector 0.3.1 says only that one is waiting"
    assert softwarepage.waiting(dict(car, update_available=None)) is None and softwarepage.waiting(None) is None

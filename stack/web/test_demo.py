import math

from demo import PLACES, VIN, export_csv, generate


def test_the_demo_export_is_six_weeks_of_a_believable_commuter():
    rows = generate(days=42, seed=7)
    assert rows[0]["vin"] == VIN and rows[0]["display_name"] == "Comet"
    drives = {r["driveNumber"] for r in rows if r["driveNumber"]}
    charges = {r["chargeNumber"] for r in rows if r["chargeNumber"]}
    assert 60 <= len(drives) <= 140 and 6 <= len(charges) <= 40, (len(drives), len(charges))
    levels = [float(r["battery_level"]) for r in rows]
    assert 15 < min(levels) and max(levels) <= 90.5, "never flat, never over the limit"
    assert any(r["fast_charger_present"] == "True" for r in rows), "one fast charge"
    assert generate(days=3, seed=7) == generate(days=3, seed=7), "deterministic"
    lat = [float(r["latitude"]) for r in rows]
    assert 39.6 < min(lat) and max(lat) < 39.9, "stays around Springfield"


def test_the_export_parses_into_drives_and_charges_the_importer_understands():
    from teslafi_import import parse, replay

    samples = parse(export_csv(generate(days=7, seed=3)))
    out = replay(samples)
    assert len(out["trips"]) >= 10 and len(out["charging_sessions"]) >= 1
    trip = out["trips"][0]
    assert trip["destination_odometer"] > trip["start_odometer"]


class _Settings:
    """A dict-backed cc_settings, enough for settings.load and settings.save."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def execute(self, sql, args=()):
        import json

        if sql.lstrip().startswith("INSERT"):
            self.rows[args[0]] = json.loads(args[1])
        data = [{"key": k, "value": v} for k, v in self.rows.items()] if sql.lstrip().startswith("SELECT") else []

        class R:
            def fetchall(self_inner):
                return data

        return R()

    def commit(self):
        pass


def test_the_demo_install_prices_its_home_charges_on_a_time_of_use_plan():
    """A fresh install has no plan, so the demo brings its own; and Home has to be named
    before the import, or no charge is at home to price."""
    from zoneinfo import ZoneInfo

    import cost
    import demo
    import settings
    from facts import charge_facts
    from places import place_for
    from teslafi_import import parse, replay

    db = _Settings()
    demo.save_plan(db)
    plan = settings.load(db)["rate_plan"]
    assert plan == demo.demo_plan() and plan["source"] == "manual" and len(plan["versions"][0]["periods"]) == 3
    cost.use_plan(plan)
    try:
        out = replay(parse(export_csv(generate(days=14, seed=7))))
        home = [{"name": n, "latitude": la, "longitude": lo, "radius_m": 150} for n, (la, lo) in PLACES.items() if n == "Home"]
        is_home = lambda lat, lon: bool(place_for(lat, lon, home))  # noqa: E731
        sessions = [dict(s, id=i) for i, s in enumerate(out["charging_sessions"])]
        facts = charge_facts(sessions, sorted(out["charging_powers"]), ZoneInfo("America/Chicago"), is_home)
        priced = [f for f in facts if f[4] and f[2] is not None]
        assert priced and all(f[9] == "plan" and f[3] == demo.demo_plan()["name"] for f in priced)
    finally:
        cost.use_plan(None)
    mine = {"name": "Mine", "source": "manual", "versions": []}
    db = _Settings({"rate_plan": mine})
    demo.save_plan(db)
    assert db.rows["rate_plan"] == mine, "an install that already has a plan keeps it"


def test_make_demo_names_the_places_and_saves_the_plan_before_it_imports():
    from pathlib import Path

    make = (Path(__file__).parents[2] / "Makefile").read_text()
    block = make[make.index("\ndemo:") :].split("\n\n")[0]
    assert block.index("demo.prepare") < block.index("teslafi_load.py"), block


def test_comet_mid_drive_is_a_drive_under_way_with_somewhere_to_be():
    """The demo media's banner: feed rows from the start of a drive to now, and the
    destination the way the car's stream sends it; read back the way Now reads them."""
    from datetime import datetime, timedelta, timezone

    import activity
    import demo
    import tesla_nav
    from teslafi_import import sample_from_row

    now = datetime(2026, 10, 6, 16, 30)
    rows, nav = demo.mid_drive(now, 70.0, 12_600.0)
    assert len(rows) == 8 and rows[-1]["Date"] == "2026-10-06 16:30:00" and all(r["carState"] == "Driving" for r in rows)
    assert all(sample_from_row(r)["driving"] for r in rows), "the importer reads them as a drive too"
    utc = timezone.utc
    readings = [
        activity.feed_row(
            {
                "at": datetime.strptime(r["Date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=utc),
                "car_state": r["carState"],
                "odometer": r["odometer"],
                "level": r["battery_level"],
                "range": r["battery_range"],
            }
        )
        for r in reversed(rows)
    ]
    first, last = activity.run(readings, "Driving", now.replace(tzinfo=utc))
    b = activity.banner({"vin": demo.VIN, "name": demo.NAME, "status": "Driving"}, "driving", (first, last), now.replace(tzinfo=utc), kwh_per_rated_mi=demo.CAPACITY_KWH / demo.RATED_MI_AT_100)
    assert b["since"] == timedelta(minutes=7) and abs(b["miles"] - demo.route("Home", "Work").miles * 7 / 11) < 0.1 and abs(b["wh_per_mi"] - demo.WH_PER_MI) < 3
    recs = [(datetime.strptime(n["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=utc), n["record"]) for n in reversed(nav)]
    assert tesla_nav.route_from_records(recs, now.replace(tzinfo=utc)) == {"name": "Work", "minutes": 4, "miles": recs[0][1]["data"][1]["value"]["doubleValue"]}


def _near_m(a, b) -> float:
    from demo import distance_mi

    return distance_mi(a, b) * 1609.34


def test_every_leg_the_demo_drives_has_a_road_between_its_places():
    import demo

    for a, b in demo.LEGS:
        road = demo.route(a, b)
        assert _near_m(road.points[0], PLACES[a]) < 50 and _near_m(road.points[-1], PLACES[b]) < 50, (a, b)
        assert 1 < road.miles < 20, (a, b, road.miles)


def test_generated_drive_positions_lie_on_the_road():
    import demo

    rows = generate(days=14, seed=7)
    by = {}
    for r in rows:
        if r["driveNumber"]:
            by.setdefault(r["driveNumber"], []).append((float(r["latitude"]), float(r["longitude"])))
    seen = set()
    for pts in by.values():
        leg = next((a, b) for a, b in demo.LEGS if _near_m(pts[0], PLACES[a]) < 50 and _near_m(pts[-1], PLACES[b]) < 50)
        seen.add(leg)
        road = demo.route(*leg)
        for p in pts:
            assert _off_road_m(p, road.points) < 1, (leg, p)
    assert len(seen) >= 4


def _off_road_m(p, points) -> float:
    """Metres from `p` to the nearest stretch of the polyline, on a flat local projection."""
    k = math.cos(math.radians(p[0]))
    best = math.inf
    for a, b in zip(points, points[1:]):
        ax, ay, bx, by = (a[1] - p[1]) * k, a[0] - p[0], (b[1] - p[1]) * k, b[0] - p[0]
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy) * 111_195)
    return best


def test_a_drive_heads_along_the_road_and_its_odometer_is_the_road_length():
    from datetime import datetime

    import demo

    road = demo.route("Home", "Work")
    (pos0, h0), (pos1, _) = road.at(0), road.at(1)
    assert pos0 == road.points[0] and pos1 == road.points[-1] and 0 <= h0 < 360
    rows = [r for r in generate(days=3, seed=7, end=datetime(2026, 9, 17, 6, 0)) if r["driveNumber"] == "1"]
    assert abs(float(rows[-1]["odometer"]) - float(rows[0]["odometer"]) - road.miles) < 0.01


def test_the_newest_full_saturday_has_a_road_charge_and_the_night_before_a_home_charge():
    """So a timeline of the demo shows both kinds of charge in its newest week, on any date."""
    from datetime import datetime

    rows = generate(days=42, seed=7, end=datetime(2026, 9, 17, 6, 0))
    fast = [r for r in rows if r["fast_charger_present"] == "True"]
    assert fast and {r["Date"][:10] for r in fast} == {"2026-09-12"}
    home = {r["Date"][:10] for r in rows if r["charging_state"] == "Charging" and r["fast_charger_present"] == "False"}
    assert "2026-09-12" in home, "a home charge in the small hours of that Saturday"


def test_years_of_history_put_a_long_life_in_front_of_the_six_weeks():
    """The pages that need years (the battery's lifetime, range per version, records) get a
    lighter history: updates every month or so, the pack fading with the miles, winters on
    their own tires, now and then a long Sunday loop."""
    from datetime import datetime

    import demo

    end = datetime(2026, 9, 17, 6, 0)
    rows = demo.generate(years=1, end=end)
    versions = []
    for r in rows:
        if not versions or versions[-1] != r["car_version"]:
            versions.append(r["car_version"])
    assert len(versions) >= 6, "an update every few weeks"
    assert all(demo._vkey(a) < demo._vkey(b) for a, b in zip(versions, versions[1:])), f"each update newer than the last: {versions}"
    six = {r["car_version"] for r in rows if r["Date"] >= "2026-08-07"}
    assert six == {versions[-1]}, "the six weeks on one version, the newest"
    assert {r["car_version"] for r in demo.generate(end=end)} == {demo.VERSION}, "no history: the six weeks on VERSION"
    # a year of fade (a few miles) is inside the estimate's own wander; three years are not
    full = [float(r["battery_range"]) / float(r["battery_level"]) * 100 for r in demo.generate(years=3, end=end) if float(r["battery_level"]) > 50]
    assert sum(full[:2000]) / 2000 > sum(full[-2000:]) / 2000 + 4, "the pack's range at 100 % fades with the miles"
    assert max(float(r["odometer"]) for r in rows) - float(rows[0]["odometer"]) > 4_000
    assert {float(r["elevation"]) for r in rows[:5000]} != {180.0}, "the ground rises and falls"
    sets = demo.tire_sets(end, 1)
    assert [n for n, *_ in sets] == ["All-season", "Winter", "All-season"] and sets[-1][2] is None
    assert demo.tire_sets(end, 0) == []
    assert demo.generate(days=42, seed=7)[-1] == demo.generate(days=42, seed=7)[-1], "still deterministic"


def test_the_six_weeks_are_the_same_six_weeks_whatever_history_comes_before():
    from datetime import datetime

    import demo

    end = datetime(2026, 9, 17, 6, 0)
    plain, long = demo.generate(end=end), demo.generate(end=end, years=1)
    schedule = lambda rows: [(r["Date"], r["driveNumber"] != "", r["speed"]) for r in rows if r["shift_state"] == "D"]  # noqa: E731
    first = plain[0]["Date"]
    assert schedule(plain) == schedule([r for r in long if r["Date"] >= first]), "the same drives at the same times and paces"


def test_the_demo_ends_today_not_on_a_fixed_date():
    from datetime import datetime, timedelta

    import demo

    assert demo.default_end(datetime(2026, 11, 3, 15, 0)) == datetime(2026, 11, 3, 6, 0)
    assert demo.default_end(datetime(2026, 11, 3, 1, 30)) == datetime(2026, 11, 3, 6, 0)
    now = datetime.now()
    last = datetime.strptime(demo.generate(days=7)[-1]["Date"], "%Y-%m-%d %H:%M:%S")
    assert last <= now and now - last < timedelta(days=2), "Now, Stats and Calendar open on live-looking data"


def test_a_demo_made_overnight_ends_at_the_last_completed_hour_with_yesterdays_rows_in_it():
    from datetime import datetime
    from unittest import mock

    import demo

    class Overnight(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 3, 2, 40)

    with mock.patch.object(demo, "datetime", Overnight):
        rows = demo.generate(days=7)
    stamps = [r["Date"] for r in rows]
    assert max(stamps) <= "2026-11-03 02:00:00", "nothing past the last completed hour"
    assert max(stamps) >= "2026-11-03 01:00:00", "and the data reaches it"
    assert sum(s >= "2026-11-02 02:00:00" for s in stamps) > 100, "the last 24 hours are full"

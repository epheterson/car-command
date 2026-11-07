"""Range at the car's recent efficiency: the window, the guards, the division."""

from datetime import datetime, timezone

from recentrange import MIN_MI, drive_energy, range_at, recent_efficiency


def test_window_takes_newest_drives_until_fifty_miles_and_keeps_the_crossing_one_whole():
    drives = [(20.0, 6.0), (20.0, 6.0), (20.0, 8.0), (30.0, 99.0)]  # the last never reached
    got = recent_efficiency(drives)
    assert got["drives"] == 3 and got["miles"] == 60.0 and round(got["wh_per_mi"]) == round(20000 / 60)


def test_too_few_miles_is_nothing():
    assert recent_efficiency([(MIN_MI - 1, 5.0)]) is None
    assert recent_efficiency([]) is None


def test_short_drives_and_missing_energy_are_skipped_not_averaged_in():
    drives = [(0.2, 0.5), (None, 3.0), (10.0, None), (10.0, 3.0), (12.0, 3.6), (0.0, 1.0)]
    got = recent_efficiency(drives)
    assert got["drives"] == 2 and got["miles"] == 22.0 and round(got["wh_per_mi"]) == 300


def test_a_bad_reading_is_not_a_figure():
    assert recent_efficiency([(30.0, 0.5)]) is None, "17 Wh/mi is a bad reading"
    assert recent_efficiency([(30.0, 80.0)]) is None, "2,667 Wh/mi too"


def test_a_window_of_hops_stops_at_the_cap():
    got = recent_efficiency([(0.6, 0.18)] * 100)
    assert got["drives"] == 60 and round(got["miles"]) == 36


def test_range_is_the_charge_over_the_rate_and_hides_without_capacity():
    assert round(range_at(50.0, 100.0, 250.0)) == 200
    assert range_at(50.0, None, 250.0) is None and range_at(50.0, 0, 250.0) is None
    assert range_at(None, 100.0, 250.0) is None and range_at(50.0, 100.0, None) is None
    assert range_at(-3.0, 100.0, 250.0) == 0.0


def test_drive_energy_prefers_the_summary_then_rated_range_then_level_drop():
    when = datetime(2026, 9, 1, tzinfo=timezone.utc)
    row = {"vin": "V1", "start_date": when, "level_start": 80.0, "level_end": 70.0, "range_start": 200.0, "range_end": 180.0, "summary_kwh": None}
    assert drive_energy(dict(row, summary_kwh=4.2), 100.0, {}, 0.25) == 4.2
    assert drive_energy(row, 100.0, {}, 0.25) == 5.0, "20 rated miles at 0.25"
    assert drive_energy(row, 100.0, {("V1", 2026): 0.3}, 0.25) == 6.0, "that year's own figure wins"
    assert drive_energy(dict(row, range_start=None), 100.0, {}, 0.25) == 10.0, "10 % of 100 kWh"
    assert drive_energy(dict(row, range_start=None), None, {}, 0.25) is None


def test_app_recent_range_reads_the_stored_fact_and_hides_without_capacity(monkeypatch):
    for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    import app

    monkeypatch.setattr(app, "cached", lambda key, ttl, load: {"V1": (250.0, 54.0)})
    got = app.recent_range("V1", 50.0, 100.0)
    assert round(got["range_recent_mi"]) == 200 and got["recent_window_mi"] == 54.0
    assert app.recent_range("V1", 50.0, None) == {"range_recent_mi": None, "recent_window_mi": None}
    assert app.recent_range("V2", 50.0, 100.0) == {"range_recent_mi": None, "recent_window_mi": None}


def test_fill_recent_facts_stores_what_the_drives_give_and_takes_the_lists_filter():
    import facts

    seen = []

    class Cur:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, sql, args=()):
            seen.append((sql, args))
            if "FROM drives" in sql:
                return Cur([{"capacity": 100.0}])
            if "FROM cc_vehicle_facts" in sql:
                return Cur([{"kwh_per_rated_mi": 0.25}])
            if "cc_vehicle_year_facts" in sql:
                return Cur([])
            if "FROM trips" in sql:
                when = datetime(2026, 9, 1, tzinfo=timezone.utc)
                return Cur([{"vin": "V1", "start_date": when, "miles": 30.0, "level_start": 80.0, "level_end": 77.0, "range_start": None, "range_end": None, "summary_kwh": None}] * 2)
            return Cur([])

    got = facts.fill_recent_facts(Conn(), "V1", None, " AND MARK")
    assert got["miles"] == 60.0 and round(got["wh_per_mi"]) == 100 and got["drives"] == 2
    assert " AND MARK" in next(s for s, _ in seen if "FROM trips" in s)
    stored = seen[-1]
    assert "INSERT INTO cc_vehicle_facts" in stored[0] and stored[1][0] == "V1" and stored[1][2] == 60.0

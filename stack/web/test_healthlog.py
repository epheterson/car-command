"""The car's own battery-health number, a row a day, held in memory for the Battery page."""

from datetime import date

import healthlog


class Conn:
    def __init__(self, rows=()):
        self.rows, self.inserts = list(rows), []

    def execute(self, sql, params=None):
        if sql.startswith("INSERT"):
            self.inserts.append(params)
        return self

    def fetchall(self):
        return self.rows


def test_a_day_is_logged_once_with_its_first_reading_and_held_only_once_committed():
    log, conn = healthlog.HealthLog(), Conn()
    entry = log.record(conn, "V1", 100.0, date(2026, 10, 14))
    assert entry == (date(2026, 10, 14), 100.0) and log.series == {}, "nothing held until the caller commits"
    assert log.due("V1", date(2026, 10, 14)), "a commit that failed leaves the day due"
    log.keep("V1", entry)
    assert log.record(conn, "V1", 99.0, date(2026, 10, 14)) is None, "the rest of the day writes nothing"
    assert not log.due("V1", date(2026, 10, 14)) and log.due("V1", date(2026, 10, 15)) and log.due("V2", date(2026, 10, 14))
    log.keep("V1", log.record(conn, "V1", 99.5, date(2026, 10, 15)))
    assert log.record(conn, "V1", None, date(2026, 10, 16)) is None, "a car that did not say"
    assert conn.inserts == [("V1", date(2026, 10, 14), 100.0), ("V1", date(2026, 10, 15), 99.5)]
    assert log.series == {"V1": [(date(2026, 10, 14), 100.0), (date(2026, 10, 15), 99.5)]}


def test_the_series_loads_once_and_picks_up_where_it_left_off():
    log = healthlog.HealthLog()
    log.load(Conn([{"vin": "V1", "day": date(2026, 10, 13), "level": 100}, {"vin": "V1", "day": date(2026, 10, 14), "level": 100}]))
    assert log.series["V1"][-1] == (date(2026, 10, 14), 100.0)
    assert not log.due("V1", date(2026, 10, 14)), "a restart does not log the day again"


def test_a_chart_once_the_number_has_moved_off_100_or_changed():
    d = date(2026, 10, 14)
    assert not healthlog.worth_charting([]) and not healthlog.worth_charting([(d, 100.0), (d, 100.0)])
    assert healthlog.worth_charting([(d, 99.0)]) and healthlog.worth_charting([(d, 100.0), (d, 100.5)])


def test_the_table_is_created_at_startup_with_the_others():
    import re
    from pathlib import Path

    app = (Path(__file__).parent / "app.py").read_text()
    assert '("battery health", healthlog.DDL)' in app and re.search(r"HEALTH\.load\(conn\)", app)

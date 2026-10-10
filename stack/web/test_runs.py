from datetime import datetime, timedelta, timezone

from runs import extend_or_start, within

T = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


class FakeConn:
    """Enough of a connection to see what the writer decides. Remembers the newest row
    per table and records every statement it was asked to run."""

    def __init__(self, newest=None):
        self.newest = newest or {}
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql.strip().split()[0], params))
        conn = self

        class Cur:
            def fetchone(self_inner):
                table = sql.split("FROM ")[1].split()[0]
                return conn.newest.get(table)

        return Cur()


def test_an_unchanged_value_stretches_the_open_run():
    conn = FakeConn({"cc_modes": {"id": 7, "sentry": True, "climate": False}})
    assert extend_or_start(conn, "cc_modes", "V", T, ("sentry", "climate"), (True, False)) == "extended"
    assert conn.statements[-1][0] == "UPDATE" and conn.statements[-1][1] == (T, 7)


def test_a_changed_value_starts_a_new_run_from_now():
    conn = FakeConn({"cc_modes": {"id": 7, "sentry": True, "climate": False}})
    assert extend_or_start(conn, "cc_modes", "V", T, ("sentry", "climate"), (True, True)) == "started"
    verb, params = conn.statements[-1]
    assert verb == "INSERT" and params == ("V", T, T, True, True)


def test_the_first_run_ever_starts_one():
    conn = FakeConn()
    assert extend_or_start(conn, "cc_modes", "V", T, ("sentry", "climate"), (False, False)) == "started"


def test_an_unknown_value_writes_nothing_at_all():
    """A row saying Sentry was off for a stretch the car had not reported is a lie with a
    timestamp on it. The Lucid publishes sentry only from v0.1.7; until then, nothing."""
    conn = FakeConn({"cc_modes": {"id": 7, "sentry": True, "climate": False}})
    assert extend_or_start(conn, "cc_modes", "V", T, ("sentry", "climate"), (None, False)) == "skipped"
    assert conn.statements == []


def test_a_drifting_float_stays_one_run_inside_the_tolerance():
    conn = FakeConn({"cc_inside_temperatures": {"id": 3, "inside_f": 71.0}})
    assert extend_or_start(conn, "cc_inside_temperatures", "V", T, ("inside_f",), (71.4,), same=within(0.5)) == "extended"
    assert extend_or_start(conn, "cc_inside_temperatures", "V", T + timedelta(minutes=1), ("inside_f",), (72.0,), same=within(0.5)) == "started"


def test_four_tire_pressures_continue_one_run_within_a_wobble():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from app import tires_same

    assert tires_same((41.3, 41.7, 41.7, 41.3), (41.5, 41.6, 41.9, 41.1))
    assert not tires_same((41.3, 41.7, 41.7, 41.3), (41.3, 41.7, 41.7, 39.9)), "a tire down 1.4 psi is a new run"
    assert not tires_same((41.3, None, 41.7, 41.3), (41.3, 41.7, 41.7, 41.3))

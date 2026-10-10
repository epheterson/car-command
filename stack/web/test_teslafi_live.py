from datetime import datetime, timedelta, timezone

from teslafi_live import TeslaFiPoller, window_start
from teslafi_import import sample_from_row

NOW = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)


def test_feed_row_becomes_a_sample_even_without_a_date():
    row = {
        "vin": "5YJ3E1EA0JF000001",
        "display_name": "Bravo",
        "state": "online",
        "shift_state": "D",
        "battery_level": "61",
        "odometer": "95400.2",
        "latitude": "40.71",
        "longitude": "-74.00",
        "charging_state": "Disconnected",
        "driveNumber": "6600",
    }
    s = sample_from_row(row, when=NOW)
    assert s["time"] == NOW and s["driving"] and s["drive_no"] == 6600 and s["odometer"] == 95400.2


def test_window_starts_at_a_sleep_and_never_before_the_import():
    rows = []
    for h in range(0, 100):
        at = NOW - timedelta(hours=100 - h)
        rows.append((at, {"vin": "V", "state": "asleep" if h % 10 == 0 else "online", "Date": ""}))
    start = window_start(rows, NOW, floor=None)
    assert start <= NOW - timedelta(hours=36) and any(at == start and r["state"] == "asleep" for at, r in rows)
    floor = NOW - timedelta(hours=20)
    assert window_start(rows, NOW, floor=floor) == floor


def test_window_reopens_to_where_the_last_good_rebuild_stopped():
    """Three days of failed rebuilds must not lose the drives in between."""
    rows = [(NOW - timedelta(hours=100 - h), {"vin": "V", "state": "asleep" if h % 10 == 0 else "online", "Date": ""}) for h in range(0, 100)]
    resume = NOW - timedelta(hours=80)
    start = window_start(rows, NOW, floor=None, resume=resume)
    assert start <= resume - timedelta(hours=1), "opens back to an hour before the last success, then to a sleep"
    assert window_start(rows, NOW, floor=None, resume=NOW - timedelta(hours=2)) <= NOW - timedelta(hours=36), "a recent success changes nothing"


def test_ingest_dedupes_and_learns_the_vin_for_a_token():
    class Conn:
        def __init__(self, store):
            self.store = store
            self.rowcount = 0

        def execute(self, sql, params=None):
            if sql.startswith("INSERT INTO cc_teslafi_rows"):
                key = (params[0], params[1])
                self.rowcount = 0 if key in self.store else 1
                self.store.add(key)
            return self

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    store = set()
    p = TeslaFiPoller(["tok"], connect=lambda: Conn(store), interval_s=60)
    row = {"vin": "V1", "Date": "2026-09-06 10:00:00", "state": "online", "battery_level": "50"}
    assert p.ingest("tok", row, NOW) is True
    assert p.ingest("tok", row, NOW) is False  # same Date: a sleeping car repeats its row
    assert p.ingest("tok", {"state": "asleep", "Date": "2026-09-06 10:01:00"}, NOW) is True  # vin remembered
    assert p.vin_for_token["tok"] == "V1"


def test_vin_token_prefix_is_understood():
    p = TeslaFiPoller(["5YJ3E1EA0JF000001:abc", " def "], connect=None)
    assert p.tokens == ["abc", "def"] and p.vin_for_token == {"abc": "5YJ3E1EA0JF000001"}


def test_use_starts_an_idle_poller_and_swaps_tokens():
    import asyncio

    async def go():
        p = TeslaFiPoller([], connect=None)
        p.run = lambda: asyncio.sleep(3600)  # the loop itself is not under test
        p.start()
        assert p.task is None, "nothing to poll, nothing started"
        p.use(["ALPHA0000000000001:tok-a"])
        assert p.tokens == ["tok-a"] and p.vin_for_token == {"tok-a": "ALPHA0000000000001"} and p.task is not None
        first = p.task
        p.use(["tok-b"])
        assert p.tokens == ["tok-b"] and p.vin_for_token == {} and p.task is first, "a running poller keeps its loop"
        first.cancel()

    asyncio.run(go())


def test_a_poll_notes_how_each_car_is_doing(monkeypatch):
    p = TeslaFiPoller(["ALPHA0000000000001:good", "BRAVO0000000000002:bad"], connect=None)
    p.ingest = lambda token, row, now: True
    p.catch_up = lambda *a, **k: None
    p.rebuild = lambda *a, **k: None
    p.caught_up = {"ALPHA0000000000001", "BRAVO0000000000002"}

    def fetch(token, command=None):
        if token == "bad":
            raise RuntimeError("401")
        return {"vin": "ALPHA0000000000001"}

    p.fetch = fetch
    p.tick()
    ok_at, err = p.results["ALPHA0000000000001"]
    assert ok_at is not None and err is None
    assert p.results["BRAVO0000000000002"][1] and "bad" not in p.results["BRAVO0000000000002"][1], "the error is kept, the token is not"


def test_a_polls_error_text_never_keeps_part_of_the_token():
    p = TeslaFiPoller(["ALPHA0000000000001:secret-token-abcdefgh"], connect=None)
    p.catch_up = lambda *a, **k: None
    p.rebuild = lambda *a, **k: None
    p.caught_up = {"ALPHA0000000000001"}

    def fetch(token, command=None):
        raise RuntimeError("x" * 100 + token)

    p.fetch = fetch
    p.tick()
    assert "secret-t" not in p.results["ALPHA0000000000001"][1]


class ChosenConn:
    """A database where a writer was chosen for the car (history_writer)."""

    def __init__(self, choice):
        self.choice, self.sql, self.row = choice, [], None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.row = (self.choice,) if sql.startswith("SELECT value FROM cc_state") and self.choice else None
        return self

    def fetchone(self):
        return self.row

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


FIXED_ROW = {"vin": "V1", "Date": "2026-09-06 10:00:00", "state": "online", "battery_level": "50", "latitude": "39.78", "longitude": "-89.65"}


def test_another_writer_chosen_the_feed_keeps_its_rows_and_writes_no_history(monkeypatch):
    import pytest

    import teslafi_live

    monkeypatch.setattr(teslafi_live, "write_history", lambda *a, **k: pytest.fail("the feed wrote a car another source writes"))
    conn = ChosenConn("tesla")
    p = TeslaFiPoller(["V1:tok"], connect=lambda: conn)
    assert p.rebuild("V1", NOW) is None and p.last_rebuild["V1"] == NOW
    assert any(s.startswith("SELECT pg_advisory_xact_lock") for s in conn.sql)
    assert p.ingest("tok", FIXED_ROW, NOW) is True, "the raw row is still kept"
    assert any(s.startswith("INSERT INTO cc_teslafi_rows") for s in conn.sql)
    assert not any(s.startswith("INSERT INTO cc_positions") for s in conn.sql), "the position is the writer's to keep"
    assert p.catch_up("V1", "tok", NOW) is None


def test_chosen_as_the_writer_the_feed_writes_positions_as_before():
    conn = ChosenConn("teslafi")
    p = TeslaFiPoller(["V1:tok"], connect=lambda: conn)
    p.ingest("tok", FIXED_ROW, NOW)
    assert any(s.startswith("INSERT INTO cc_positions") for s in conn.sql)


def test_a_drive_or_charge_that_ends_is_written_on_the_next_poll_not_the_hour():
    p = TeslaFiPoller(["ALPHA0000000000001:tok"], connect=None)
    p.ingest = lambda token, row, now: True
    p.catch_up = lambda *a, **k: None
    p.caught_up = {"ALPHA0000000000001"}
    rebuilt = []

    def rebuild(vin, now):  # as the real one, it notes when it ran
        rebuilt.append(vin)
        p.last_rebuild[vin] = now

    p.rebuild = rebuild
    states = iter(["Idling", "Driving", "Driving", "Idling", "Idling", "Charging", "Sleeping"])
    p.fetch = lambda token, command=None: {"vin": "ALPHA0000000000001", "carState": next(states)}
    p.tick()
    assert rebuilt == ["ALPHA0000000000001"], "the first poll rebuilds, as before"
    for _ in range(2):
        p.tick()
    assert len(rebuilt) == 1, "mid-drive: nothing until the hour"
    p.tick()
    assert len(rebuilt) == 2, "the drive ended: written now"
    p.tick()
    assert len(rebuilt) == 2
    p.tick()
    p.tick()
    assert len(rebuilt) == 3, "a charge that ended, the same"

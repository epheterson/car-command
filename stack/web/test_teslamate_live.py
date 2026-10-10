from datetime import datetime, timedelta, timezone

from teslamate_live import TeslaMatePoller, import_id, model_name, window_start

NOW = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)


def test_the_model_reads_from_teslamates_letter_and_trim():
    assert model_name({"model": "3", "trim_badging": "Long Range"}) == "Model 3 Long Range"
    assert model_name({"model": "y"}) == "Model Y" and model_name({"model": "", "trim_badging": "x"}) is None


def test_the_window_opens_back_to_the_last_success_and_never_before_the_floor():
    assert window_start(NOW, None, None) == NOW - timedelta(hours=36)
    assert window_start(NOW, None, NOW - timedelta(days=4)) == NOW - timedelta(days=4, hours=1)
    assert window_start(NOW, NOW - timedelta(hours=2), None) == NOW - timedelta(hours=2)


class FakeConn:
    def __init__(self):
        self.written = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO cc_positions"):
            self.written.append(params)
        return self

    def fetchone(self):
        return None  # no writer chosen: TeslaMate writes, as it always has

    def commit(self):
        pass


def test_teslamates_own_drives_and_charges_are_read_for_a_handover():
    start = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(minutes=10)  # under way now

    class TM:
        def __init__(self):
            self.sql = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql.append((sql, params))
            self.rows = (
                [{"id": 3, "vin": "V1", "name": "Alpha", "model": "3", "trim_badging": None}]
                if "FROM cars" in sql
                else [{"start_date": start, "end_date": None}] if "FROM drives" in sql else [{"start_date": start - timedelta(hours=3), "end_date": start - timedelta(hours=2)}]
            )
            return self

        def fetchall(self):
            return self.rows

    tm = TM()
    p = TeslaMatePoller("postgresql://x", connect=None, connect_teslamate=lambda: tm)
    since = (start - timedelta(hours=4)).replace(tzinfo=timezone.utc)
    got = p.spans("V1", since)
    assert got[0][0] == start.replace(tzinfo=timezone.utc) and got[0][1] > got[0][0], "a drive under way ends now"
    assert got[1] == ((start - timedelta(hours=3)).replace(tzinfo=timezone.utc), (start - timedelta(hours=2)).replace(tzinfo=timezone.utc))
    assert all(params == (3, since.replace(tzinfo=None)) for sql, params in tm.sql if "car_id" in sql), "TeslaMate's columns are naive UTC"
    assert p.spans("V9", datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)) == []


def test_the_newest_position_is_written_once_in_miles():
    conn = FakeConn()
    p = TeslaMatePoller("postgresql://x", connect=lambda: conn)
    pos = {"date": datetime(2026, 9, 17, 19, 0), "latitude": 39.78, "longitude": -89.65, "odometer": 100.0, "battery_level": 61, "speed": 50, "drive_id": 7}
    assert p.record_position("V1", pos, NOW) is True
    assert p.record_position("V1", pos, NOW) is False, "the same row is not written twice"
    vin, at, lat, lon, level, odo, state, speed, imp = conn.written[0]
    assert at.tzinfo is not None and abs(odo - 62.1371) < 0.001 and state == "DRIVING" and abs(speed - 31.07) < 0.01 and imp == import_id("V1")


def test_a_poller_without_a_url_does_not_start():
    p = TeslaMatePoller("", connect=lambda: None)
    p.start()
    assert p.task is None


def test_teslamate_follows_only_the_chosen_cars_and_takes_a_new_url():
    p = TeslaMatePoller("", connect=lambda: None)
    p.use("postgresql://x", ["ALPHA0000000000001"])
    assert p.db_url == "postgresql://x" and p.vins == ["ALPHA0000000000001"]
    cars = [{"id": 1, "vin": "ALPHA0000000000001"}, {"id": 2, "vin": "BRAVO0000000000002"}]
    assert [c["vin"] for c in p.followed(cars)] == ["ALPHA0000000000001"]
    p.use("postgresql://x", [])
    assert len(p.followed(cars)) == 2, "no choice made follows every car"

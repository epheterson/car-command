"""The data checks: they read the data, not the markers."""

from datetime import datetime, timedelta, timezone

from health import HealthWorker, check, decode, encode, gather

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
D = timedelta(days=1)


def kitt(**over):
    """Alpha, shaped like the morning of 2026-09-28: feed current, rebuild ok, nothing logged in 27 days."""
    car = {
        "vin": "V1",
        "name": "Alpha",
        "source": "feed",
        "raw_newest": NOW - 5 * timedelta(minutes=1),
        "derived_newest": NOW - 27 * D,
        "active_since_derived": True,
        "odo_7d": 114.6,
        "trips_7d": 0.0,
        "rebuild": f"ok {(NOW - H).isoformat()} trips=0 from=...",
    }
    car.update(over)
    return car


def test_the_erosion_case_is_two_findings():
    kinds = {f["kind"]: f for f in check(kitt(), NOW)}
    assert set(kinds) == {"stale", "miles"}
    assert "nothing has been logged" in kinds["stale"]["text"] and kinds["stale"]["level"] == "bad"
    assert kinds["miles"]["text"].startswith("115 of the 115 mi Alpha drove this week")
    assert kinds["miles"]["odo_7d"] == 114.6, "the evidence travels with the finding"


def test_a_healthy_feed_car_is_quiet():
    assert check(kitt(derived_newest=NOW - 2 * H, trips_7d=112.0), NOW) == []


def test_a_parked_week_is_not_stale():
    # readings arrive, nothing logged for a week, but the car never moved: fine
    assert check(kitt(derived_newest=NOW - 9 * D, active_since_derived=False, odo_7d=0.0, trips_7d=0.0), NOW) == []


def test_short_gaps_are_ignored():
    assert check(kitt(derived_newest=NOW - H, odo_7d=100.0, trips_7d=96.0), NOW) == [], "under five miles"
    assert check(kitt(derived_newest=NOW - H, odo_7d=100.0, trips_7d=85.0), NOW) == [], "under a fifth"
    assert [f["kind"] for f in check(kitt(derived_newest=NOW - H, odo_7d=100.0, trips_7d=70.0), NOW)] == ["miles"]


def test_a_silent_source_is_one_finding_only():
    out = check(kitt(raw_newest=NOW - 3 * D), NOW)
    assert [f["kind"] for f in out] == ["quiet"] and "3 days" in out[0]["text"]
    assert check(kitt(raw_newest=None), NOW)[0]["text"].startswith("Nothing has ever arrived")


def test_rebuild_marker_is_read():
    ok = kitt(derived_newest=NOW - H, trips_7d=114.0)
    assert check(dict(ok, rebuild=f"error {(NOW - H).isoformat()} relation does not exist"), NOW)[0]["kind"] == "rebuild"
    late = check(dict(ok, rebuild=f"ok {(NOW - 5 * H).isoformat()} trips=1 from=x"), NOW)
    assert late and late[0]["level"] == "warn" and "5 h" in late[0]["text"]
    assert check(dict(ok, rebuild="garbage"), NOW) == [], "an unreadable marker is not a finding by itself"


def test_history_only_and_live_cars():
    assert check({"vin": "V2", "name": "Bravo", "source": "history", "raw_newest": None}, NOW) == [], "nothing is expected to arrive"
    live = {"vin": "V3", "name": "Charlie", "source": "live", "raw_newest": NOW - H, "derived_newest": NOW - 3 * D, "active_since_derived": True, "odo_7d": 215.5, "trips_7d": 218.2}
    assert check(live, NOW) == [], "the connector writes its own drives; only the miles check applies"
    assert [f["kind"] for f in check(dict(live, trips_7d=100.0), NOW)] == ["miles"]


def test_round_trip_keeps_datetimes_readable():
    car = kitt()
    got = decode(encode(car, check(car, NOW)))
    assert got["car"]["raw_newest"] == car["raw_newest"].isoformat() and got["findings"][0]["kind"] == "stale"
    assert decode(None) is None and decode("{not json") is None


class Conn:
    """Answers each query by the table it names; records writes."""

    def __init__(self, answers: dict):
        self.answers, self.writes, self.last = answers, [], None

    def execute(self, sql, params=None):
        self.last = None
        if sql.startswith("INSERT") or sql.startswith("CREATE"):
            self.writes.append((sql, params))
            return self
        for needle, value in self.answers.items():
            if needle in sql or any(needle in str(p) for p in (params or ())):
                self.last = value
                break
        return self

    def fetchone(self):
        return None if self.last is None else (self.last,)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_gather_reads_the_feed_shape():
    conn = Conn({"MAX(at) FROM cc_teslafi_rows": NOW - H, "(data->>'odometer')": 114.6, "feed_rebuild": "ok x", "GREATEST": NOW - 27 * D, "SUM(destination_odometer": 0.0, "carState": 1})
    car = gather(conn, "V1", "Alpha", "feed", NOW)
    assert car["raw_newest"] == NOW - H and car["odo_7d"] == 114.6 and car["active_since_derived"] is True and car["rebuild"] == "ok x"
    live = gather(Conn({"FROM states": NOW, "MAX(odometer) - MIN(odometer)": 10.0, "GREATEST": NOW, "SUM(destination_odometer": 9.0}), "V3", "Charlie", "live", NOW)
    assert live["raw_newest"] == NOW and live["odo_7d"] == 10.0 and "rebuild" not in live


def test_worker_stores_the_answer_and_alerts_once_a_day():
    sent = []
    conn = Conn({"MAX(at) FROM cc_teslafi_rows": NOW - H, "(data->>'odometer')": 114.6, "GREATEST": NOW - 27 * D, "SUM(destination_odometer": 0.0, "carState": 1})
    w = HealthWorker(connect=lambda: conn, cars=lambda: [{"vin": "V1", "name": "Alpha", "source": "feed"}], notify=lambda text, kind: sent.append((kind, text)))
    assert w.round(NOW) == 2
    assert [k for k, _ in sent] == ["data", "data"]
    stored = [p for s, p in conn.writes if p and p[0] == "health:V1"]
    assert stored and decode(stored[-1][1])["findings"][0]["kind"] == "stale"
    # the second round the same day: the markers say sent, nothing goes out again
    conn.answers["health_sent:"] = NOW.date().isoformat()
    w.round(NOW)
    assert len(sent) == 2

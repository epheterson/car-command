from datetime import datetime, timedelta, timezone

from weather import ARCHIVE, FORECAST, WeatherWorker, describe, endpoint, params, pick

NOW = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)


def test_describe_speaks_plainly_and_mentions_wind_only_when_it_mattered():
    assert describe(61) == "light rain"
    assert describe(0, wind_kmh=10) == "clear"
    assert describe(3, wind_kmh=40) == "overcast, 25 mph wind"
    assert describe(3, wind_kmh=40, metric=True) == "overcast, 40 km/h wind"
    assert describe(None) is None and describe(12345, wind_kmh=50) == "31 mph wind"


def test_recent_hours_come_from_the_forecast_api_and_old_ones_from_the_archive():
    assert endpoint(NOW - timedelta(days=1), NOW) == FORECAST
    assert endpoint(NOW - timedelta(days=30), NOW) == ARCHIVE
    old = params(39.78191, -89.65063, NOW - timedelta(days=30), NOW)
    assert old["latitude"] == 39.78 and old["longitude"] == -89.65, "two decimals: about a kilometre, and nothing that says which house"
    assert old["start_date"] == old["end_date"] == "2026-08-18" and "past_days" not in old
    assert params(39.8, -89.6, NOW - timedelta(hours=3), NOW)["past_days"] == 7


def test_pick_takes_the_nearest_hour_and_refuses_a_far_one():
    hourly = {
        "time": ["2026-09-17T18:00", "2026-09-17T19:00", "2026-09-17T20:00"],
        "temperature_2m": [20.1, 21.0, 22.3],
        "precipitation": [0, 0.4, 0],
        "wind_speed_10m": [8, 12, 35],
        "weather_code": [1, 61, 3],
    }
    assert pick(hourly, datetime(2026, 9, 17, 19, 20, tzinfo=timezone.utc)) == {"temp_c": 21.0, "precip_mm": 0.4, "wind_kmh": 12, "code": 61}
    assert pick(hourly, datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)) is None
    assert pick({}, NOW) is None


class FakeConn:
    def __init__(self, pending):
        self.pending, self.written, self.committed = pending, [], 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        conn = self

        class Cur:
            def fetchall(self_inner):
                return conn.pending

        if sql.lstrip().startswith("INSERT INTO cc_weather"):
            self.written.append(params)
        return Cur()

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


def test_a_round_fills_each_pending_drive_from_one_request_and_stops_on_a_failure():
    start = NOW - timedelta(hours=5)
    pending = [
        {"vin": "V1", "start_date": start, "lat": 39.8, "lon": -89.6},
        {"vin": "V1", "start_date": start - timedelta(days=1), "lat": 39.8, "lon": -89.6},
        {"vin": "V2", "start_date": start, "lat": 1.0, "lon": 1.0},
    ]
    calls = []

    def fetch(url, query):
        calls.append((url, query))
        if query["latitude"] == 1.0:
            raise RuntimeError("429")
        t = start.replace(minute=0)
        return {
            "hourly": {
                "time": [(t + timedelta(hours=k) - timedelta(days=1 if query.get("latitude") == 39.8 and len(calls) == 2 else 0)).strftime("%Y-%m-%dT%H:%M") for k in range(2)],
                "temperature_2m": [18.0, 18.5],
                "precipitation": [0.0, 0.0],
                "wind_speed_10m": [5.0, 6.0],
                "weather_code": [0, 1],
            }
        }

    conn = FakeConn(pending)
    w = WeatherWorker(connect=lambda: conn, fetch=fetch, enabled=lambda: True, pause_s=0)
    assert w.round(NOW) == 2
    assert len(conn.written) == 2 and conn.written[0][0] == "V1" and conn.written[0][5] == 0
    assert len(calls) == 3, "the failing third request ends the batch; it is retried next round"
    assert WeatherWorker(connect=lambda: conn, fetch=fetch, enabled=lambda: False, pause_s=0).round(NOW) == 0, "off on Settings means no request leaves"

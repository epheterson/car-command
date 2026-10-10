"""Tesla's cloud as the Powerwall's backup source: the live fallback and the history backfill, against fakes."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import energy
import energy_cloud
import tesla_fleet
from tesla_fleet import TeslaError

NOW = datetime(2026, 7, 14, 19, 3, 20, tzinfo=timezone.utc)


def test_a_live_status_becomes_a_reading_on_the_gateways_signs():
    got = energy_cloud.reading_from_live({"solar_power": 4000.0, "battery_power": -1500.0, "grid_power": 200.0, "load_power": 2700.0, "percentage_charged": 62.0, "grid_status": "Active"})
    assert got == {"solar_w": 4000.0, "battery_w": -1500.0, "grid_w": 200.0, "home_w": 2700.0, "soe_pct": 62.0, "grid_status": "Active"}
    assert energy_cloud.reading_from_live({"solar_power": 1000, "battery_power": 0, "grid_power": 500})["home_w"] == 1500, "the load is what the sources add up to"
    assert energy_cloud.reading_from_live({})["solar_w"] is None


def test_a_series_becomes_utc_rows_and_a_damaged_sample_is_skipped():
    series = [
        {"timestamp": "2026-07-14T00:00:00-07:00", "solar_power": 0, "battery_power": 1200.5, "grid_power": 10, "grid_services_power": 0},
        {"timestamp": "2026-07-14T00:05:00-07:00", "solar_power": 0, "battery_power": None, "grid_power": 10},
        {"timestamp": "yesterday", "solar_power": 5},
        {"solar_power": 5},
        "junk",
        {"timestamp": "2026-07-14T12:05:00-07:00", "solar_power": 6000, "battery_power": -2000, "grid_power": -500},
    ]
    rows = energy_cloud.rows_from_series(series)
    assert [r["at"] for r in rows] == [datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc), datetime(2026, 7, 14, 7, 5, tzinfo=timezone.utc), datetime(2026, 7, 14, 19, 5, tzinfo=timezone.utc)]
    assert rows[0]["home_w"] == pytest.approx(1210.5) and rows[0]["soe_pct"] is None
    assert rows[1]["home_w"] == 10 and rows[2]["home_w"] == 3500


class Fleet:
    def __init__(self, live=None, days=None, installed="2026-07-01", fail=None):
        self.live_body, self.days, self.installed_on, self.fail = live or {}, days or {}, installed, fail
        self.asked = []

    def energy_live(self, access, base, site):
        self.asked.append("live")
        if self.fail:
            raise self.fail
        return self.live_body

    def energy_installed(self, access, base, site):
        return self.installed_on

    def energy_power_day(self, access, base, site, day, tz):
        self.asked.append(day)
        if self.fail and not isinstance(self.fail, str):
            raise self.fail
        return self.days.get(day, [])


def day_series(day):
    return [{"timestamp": f"{day.isoformat()}T{h:02d}:{m:02d}:00+00:00", "solar_power": 100, "battery_power": 0, "grid_power": 50} for h in range(24) for m in range(0, 60, 5)]


class State(dict):
    def put(self, key, value):
        self[key] = value


def cloud(fleet, state=None, **kw):
    state = state if state is not None else State()
    kw.setdefault("now", lambda: datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc))
    live, written = energy.Live(), []
    w = energy_cloud.CloudWorker(
        fleet=lambda: fleet,
        access=lambda: ("tok", tesla_fleet.NA),
        site=lambda: "1234567890",
        tz="America/Los_Angeles",
        get=state.get,
        put=state.put,
        write=lambda rows: written.extend(rows),
        have_day=kw.pop("have_day", lambda day: False),
        live=live,
        sleep=lambda s: None,
        **kw,
    )
    return w, state, live, written


# ---- the live fallback


def test_the_cloud_is_asked_for_live_status_at_most_every_five_minutes():
    f = Fleet(live={"solar_power": 100, "battery_power": 0, "grid_power": 50, "load_power": 150, "percentage_charged": 70})
    w, _s, live, written = cloud(f)
    w.fallback(NOW)
    assert f.asked == ["live"] and live.get(NOW)["source"] == "cloud" and live.get(NOW)["solar_w"] == 100
    assert written and written[0]["at"] == NOW.replace(minute=0, second=0), "on the five-minute grid, so history upserts over it"
    w.fallback(NOW + timedelta(minutes=4))
    assert f.asked == ["live"], "five minutes at most"
    w.fallback(NOW + timedelta(minutes=5, seconds=1))
    assert f.asked == ["live", "live"]


def test_a_refusal_is_remembered_for_settings_and_does_not_raise():
    w, _s, live, written = cloud(Fleet(fail=TeslaError("Tesla refused (403)", 403)))
    w.fallback(NOW)
    assert written == [] and live.get(NOW) is None
    assert w.status["ok"] is False and w.status["scope"] is True and "403" not in w.status["error"], "Settings words it, not Tesla"


# ---- the backfill


def test_a_backfill_starts_at_yesterday_and_knows_how_many_days_it_is():
    f = Fleet(installed="2026-07-01")
    w, state, *_ = cloud(f)
    prog = w.start_backfill(date(2026, 7, 14))
    assert prog["state"] == "running" and prog["next"] == "2026-07-14" and prog["stop"] == "2026-07-01" and prog["total"] == 14 and prog["done"] == 0
    assert json.loads(state[energy_cloud.BACKFILL_KEY])["state"] == "running", "kept, so a restart resumes it"


def test_each_step_is_one_day_newest_first_and_the_rows_are_kept():
    days = {date(2026, 7, 14): day_series(date(2026, 7, 14)), date(2026, 7, 13): day_series(date(2026, 7, 13))}
    f = Fleet(days=days, installed="2026-07-13")
    w, state, _l, written = cloud(f)
    w.start_backfill(date(2026, 7, 14))
    assert w.backfill_step() == "running"
    assert f.asked == [date(2026, 7, 14)] and len(written) == 288
    assert w.backfill_step() == "done"
    assert f.asked == [date(2026, 7, 14), date(2026, 7, 13)]
    prog = energy_cloud.progress(state.get)
    assert prog["state"] == "done" and prog["done"] == 2 and prog["total"] == 2


def test_a_day_already_held_is_not_asked_for_again():
    f = Fleet(installed="2026-07-13")
    w, *_ = cloud(f, have_day=lambda d: d == date(2026, 7, 14))
    w.start_backfill(date(2026, 7, 14))
    w.backfill_step()
    assert f.asked == [], "a day with its readings already in costs no request"
    assert energy_cloud.progress(w.get)["next"] == "2026-07-13"


def test_without_an_install_date_it_stops_after_a_month_with_nothing():
    f = Fleet(installed=None, days={})
    w, state, *_ = cloud(f)
    w.start_backfill(date(2026, 7, 14))
    states = [w.backfill_step() for _ in range(energy_cloud.EMPTY_STOP + 2)]
    assert states[-1] == "done" and states.count("running") == energy_cloud.EMPTY_STOP - 1
    assert len(f.asked) == energy_cloud.EMPTY_STOP


def test_a_rate_limit_backs_off_exponentially_without_asking_again_sooner():
    f = Fleet(fail=TeslaError("slow down", 429), installed="2026-07-01")
    now = [datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)]
    w, state, *_ = cloud(f, now=lambda: now[0])
    w.start_backfill(date(2026, 7, 14))
    waits = []
    for _ in range(8):
        assert w.backfill_step() == "running"
        prog = energy_cloud.progress(state.get)
        waits.append(int((datetime.fromisoformat(prog["retry_at"]) - now[0]).total_seconds()))
        asked = len(f.asked)
        assert w.backfill_step() == "running" and len(f.asked) == asked, "inside the wait nothing is asked"
        now[0] += timedelta(seconds=waits[-1] + 1)
    assert waits == [60, 120, 240, 480, 960, 1800, 1800, 1800], "60 s doubling to 30 minutes"
    prog = energy_cloud.progress(state.get)
    assert prog["next"] == "2026-07-14" and prog["done"] == 0
    f.fail = None
    f.days = {date(2026, 7, 14): day_series(date(2026, 7, 14))}
    assert w.backfill_step() == "running"
    assert energy_cloud.progress(state.get)["next"] == "2026-07-13" and energy_cloud.progress(state.get)["retry_at"] is None, "a good day clears the wait"


def test_a_refusal_pauses_with_the_reason():
    f = Fleet(fail=TeslaError("forbidden", 403), installed="2026-07-01")
    w, state, *_ = cloud(f)
    w.start_backfill(date(2026, 7, 14))
    assert w.backfill_step() == "error" and "sign in" in energy_cloud.progress(state.get)["error"].lower()
    assert w.backfill_step() == "error" and len(f.asked) == 1, "an error is not asked again until resumed"


def test_anything_unexpected_is_an_error_state_not_a_hot_loop():
    class Odd(Fleet):
        def energy_power_day(self, *a):
            self.asked.append("odd")
            raise ValueError("no")

    f = Odd(installed="2026-07-01")
    w, state, *_ = cloud(f)
    w.start_backfill(date(2026, 7, 14))
    assert w.backfill_step() == "error" and energy_cloud.progress(state.get)["error"]
    assert w.backfill_step() == "error" and f.asked == ["odd"]


def test_a_write_that_fails_does_not_advance_and_waits_out_the_backoff():
    days = {date(2026, 7, 14): day_series(date(2026, 7, 14))}
    f = Fleet(days=days, installed="2026-07-01")
    now = [datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)]
    w, state, *_ = cloud(f, now=lambda: now[0])
    boom = [True]

    def write(rows):
        if boom[0]:
            raise OSError("disk")

    w.write = write
    w.start_backfill(date(2026, 7, 14))
    assert w.backfill_step() == "running"
    prog = energy_cloud.progress(state.get)
    assert prog["next"] == "2026-07-14" and prog["done"] == 0 and prog["retry_at"]
    asked = len(f.asked)
    assert w.backfill_step() == "running" and len(f.asked) == asked
    now[0] += timedelta(seconds=61)
    boom[0] = False
    assert w.backfill_step() == "running" and energy_cloud.progress(state.get)["next"] == "2026-07-13"


def test_stopping_keeps_the_place_and_starting_again_resumes_from_it():
    f = Fleet(days={date(2026, 7, 14): day_series(date(2026, 7, 14))}, installed="2026-07-01")
    w, state, *_ = cloud(f)
    w.start_backfill(date(2026, 7, 14))
    w.backfill_step()
    w.stop_backfill()
    assert energy_cloud.progress(state.get)["state"] == "paused" and energy_cloud.progress(state.get)["next"] == "2026-07-13"
    again = w.start_backfill(date(2026, 7, 20))
    assert again["next"] == "2026-07-13" and again["done"] == 1 and again["state"] == "running", "resumed, not restarted"


def test_the_cost_note_counts_days_and_says_it_is_an_estimate():
    note = energy_cloud.cost_note(1000)
    assert "1,000" in note and "$" in note and "estimate" in note.lower()
    assert energy_cloud.days_between("2024-03-01", date(2026, 7, 14)) == 866


def test_a_backfill_waiting_out_a_back_off_sleeps_until_it_is_over_without_reading_the_database():
    f = Fleet(fail=TeslaError("slow down", 429), installed="2026-07-01")
    now = [datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)]
    w, state, *_ = cloud(f, now=lambda: now[0])
    w.start_backfill(date(2026, 7, 14))
    w.backfill_step()
    assert 59 <= w.wait_s() <= 60, "the time left, from the kept retry_at"
    now[0] += timedelta(seconds=45)
    assert 14 <= w.wait_s() <= 15
    now[0] += timedelta(seconds=60)
    assert w.wait_s() == 0.0
    w.stop_backfill()
    assert w.wait_s() == 0.0


@pytest.mark.parametrize("minute,second,floor", [(0, 0, 0), (3, 20, 0), (5, 0, 5), (9, 59, 5), (59, 59, 55)])
def test_live_rows_sit_on_the_five_minute_grid(minute, second, floor):
    f = Fleet(live={"solar_power": 100, "battery_power": 0, "grid_power": 50})
    w, _s, _l, written = cloud(f)
    w.fallback(NOW.replace(minute=minute, second=second))
    assert written[0]["at"] == NOW.replace(minute=floor, second=0, microsecond=0)

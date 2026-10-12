"""The house's energy: config, minute means, and the split of a charge between sun, Powerwall and grid."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import cost
import energy

PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)  # noon Pacific


def row(minute, solar=0.0, home=0.0, grid=0.0, battery=0.0, source="cloud"):
    return {"at": T0 + timedelta(minutes=minute), "solar_w": solar, "home_w": home, "grid_w": grid, "battery_w": battery, "soe_pct": 50.0, "source": source}


# ---- config


class State(dict):
    def put(self, key, value):
        self[key] = value


def test_config_has_defaults_and_ignores_damage():
    state = State()
    assert energy.load_config(state.get) == {"cloud": {"enabled": False, "site_id": "", "name": "", "installed": ""}}
    state[energy.CONFIG_KEY] = "{not json"
    assert energy.load_config(state.get)["cloud"]["enabled"] is False
    cfg = energy.load_config(state.get)
    cfg["cloud"].update(enabled=True, site_id="1234567890", name="Springfield", stray="x")
    energy.save_config(state.put, cfg)
    again = energy.load_config(state.get)
    assert again["cloud"]["site_id"] == "1234567890" and "stray" not in again["cloud"]


def test_what_a_local_gateway_left_in_the_state_is_deleted_once_and_the_rest_of_the_config_kept():
    state = State(
        {
            energy.CONFIG_KEY: '{"local": {"url": "https://192.0.2.10", "fingerprint": "ab:cd", "env_pin": {}}, "cloud": {"enabled": true, "site_id": "7", "name": "Springfield", "installed": ""}}',
            energy.LEGACY_PASSWORD_KEY: "sealed",
        }
    )
    gone = []
    assert energy.drop_gateway(state.get, state.put, lambda k: (gone.append(k), state.pop(k)))
    assert gone == [energy.LEGACY_PASSWORD_KEY] and "local" not in state[energy.CONFIG_KEY] and "192.0.2.10" not in state[energy.CONFIG_KEY]
    assert energy.load_config(state.get)["cloud"]["site_id"] == "7"
    assert not energy.drop_gateway(state.get, state.put, lambda k: state.pop(k)), "nothing left to do the second time"
    empty = State()
    assert not energy.drop_gateway(empty.get, empty.put, lambda k: empty.pop(k)) and empty == {}, "an install that never had one is left alone"


# ---- readings


def test_home_is_what_the_sources_add_up_to():
    assert energy.home_from(1000, -200, 300) == 1100 and energy.home_from(1000, None, 0) is None and energy.home_from(0, -500, 0) == 0.0


def test_live_holds_the_newest_reading_and_forgets_an_old_one():
    live = energy.Live()
    now = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
    assert live.get(now) is None
    live.set({"solar_w": 5}, "cloud", now)
    assert live.get(now + timedelta(seconds=30)) == {"solar_w": 5, "source": "cloud", "age_s": 30}
    assert live.get(now + timedelta(seconds=200))["age_s"] == 200, "readings are five minutes apart: one is good for fifteen"
    assert live.get(now + timedelta(seconds=energy.LIVE_MAX_AGE_S + 1)) is None


# ---- rows


class Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def executemany(self, sql, rows):
        self.log.append((sql, list(rows)))


class Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return Cursor(self.log)


def test_a_later_row_for_the_same_time_replaces_the_earlier():
    conn = Conn()
    assert energy.store(conn, [row(0)]) == 1 and energy.store(conn, [], "cloud") == 0
    sql, rows = conn.log[0]
    assert "ON CONFLICT (site, at) DO UPDATE" in sql and "WHERE" not in sql
    assert rows[0][-1] == "cloud" and rows[0][0] == energy.SITE


def test_a_row_stands_for_five_minutes_and_where_two_overlap_the_later_one_is_read():
    early, late = row(0, solar=100), row(3, solar=900)
    got = energy.minute_samples([late, early])
    assert [got[T0 + timedelta(minutes=i)]["solar_w"] for i in range(8)] == [100, 100, 100, 900, 900, 900, 900, 900]
    assert T0 + timedelta(minutes=8) not in got


# ---- the car's share of a minute: what plugging it in drew


def test_the_car_gets_only_the_surplus_sun_the_house_was_not_using():
    # 9/21 13:51-14:56 as it happened: solar about 3.3 kW, the house before the car 2.8 kW, the car 7.5 kW, and the grid
    # making up the rest. The car did not run on the house's 32% sun: it took the 0.5 kW that had been going to the Powerwall.
    sun, bat, grid = energy.car_mix({"solar_w": 3300, "home_w": 10300, "battery_w": 0, "grid_w": 7000}, 7500)
    assert sun == pytest.approx(0.5 / 7.5) and bat == 0 and grid == pytest.approx(7.0 / 7.5)


def test_a_midday_charge_with_a_big_surplus_is_all_sun():
    assert energy.car_mix({"solar_w": 9000, "home_w": 8000, "battery_w": 0, "grid_w": -1000}, 7000) == (1.0, 0.0, 0.0), "base 1 kW, 8 kW of surplus, the car wants 7"


def test_at_night_the_car_has_no_sun():
    assert energy.car_mix({"solar_w": 0, "home_w": 9000, "battery_w": 0, "grid_w": 9000}, 7000) == (0.0, 0.0, 1.0)


def test_what_the_sun_does_not_cover_is_shared_by_battery_and_grid_as_they_were_giving():
    sun, bat, grid = energy.car_mix({"solar_w": 0, "home_w": 10000, "battery_w": 2000, "grid_w": 2000}, 6000)
    assert (sun, bat, grid) == pytest.approx((0.0, 0.5, 0.5))
    sun, bat, grid = energy.car_mix({"solar_w": 1000, "home_w": 9000, "battery_w": 5000, "grid_w": 3000}, 6000)
    assert sun == pytest.approx(0.0), "the house's own 3 kW takes the sun first"
    assert (bat, grid) == pytest.approx((5 / 8, 3 / 8))
    assert energy.car_mix({"solar_w": 0, "home_w": 9000, "battery_w": 0, "grid_w": 0}, 6000) == (0.0, 0.0, 1.0), "nothing reported giving it: the grid, as the safe reading"


def test_a_car_larger_than_the_houses_five_minute_average_is_clamped_to_it():
    # a minute of 7 kW against a five-minute house average of 5 kW: the car cannot have had more than the house had
    sun, bat, grid = energy.car_mix({"solar_w": 2000, "home_w": 5000, "battery_w": 0, "grid_w": 3000}, 7000)
    assert (sun, bat, grid) == pytest.approx((0.4, 0.0, 0.6)) and sum((sun, bat, grid)) == pytest.approx(1.0)
    sun, bat, grid = energy.car_mix({"solar_w": 12000, "home_w": 5000, "battery_w": 0, "grid_w": -7000}, 7000)
    assert (sun, bat, grid) == (1.0, 0.0, 0.0), "sun that was exported is not the house's"


def test_a_minute_with_nothing_readable_has_no_mix():
    assert energy.car_mix({"solar_w": 100, "home_w": 0}, 7000) is None and energy.car_mix({"solar_w": 100}, 7000) is None
    assert energy.car_mix({"solar_w": 100, "home_w": 5000}, 0) is None and energy.car_mix({"solar_w": None, "home_w": 5000, "battery_w": None, "grid_w": 5000}, 3000) == (0.0, 0.0, 1.0)


def test_each_minute_of_a_charge_is_judged_against_its_own_house_and_car():
    draw = [(T0, T0 + timedelta(minutes=10), 7.2)]
    rows = [row(0, solar=3300, home=10000, grid=6700), row(5, solar=0, home=9000, grid=9000)]
    split = energy.split_charge(draw, T0, T0 + timedelta(minutes=10), rows)
    # first five minutes: base 2.8, surplus 0.5 -> 0.5 of 7.2 is sun; the second five: none
    assert split["solar"] == pytest.approx(5 * 0.5 / 60) and split["grid"] == pytest.approx(1.2 - 5 * 0.5 / 60) and split["battery"] == 0


# ---- one charge


def test_car_minutes_integrate_power_into_whole_minutes():
    got = energy.car_minutes([(T0 - timedelta(minutes=5), T0 + timedelta(seconds=90), 12.0), (T0 + timedelta(seconds=90), T0 + timedelta(minutes=10), 0.0)], T0, T0 + timedelta(minutes=10))
    assert got == {T0: pytest.approx(0.2), T0 + timedelta(minutes=1): pytest.approx(0.1)}


def test_a_charge_in_full_sun_is_all_sun_and_one_after_dark_is_all_grid():
    day = [row(i, solar=9000, home=8000, grid=-1000) for i in range(30)]
    split = energy.split_charge([(T0, T0 + timedelta(minutes=30), 6.0)], T0, T0 + timedelta(minutes=30), day)
    assert split["solar"] == pytest.approx(3.0) and split["battery"] == 0 and split["grid"] == 0 and split["covered"] == pytest.approx(1.0)
    night = [row(i, solar=0, home=6500, grid=6500) for i in range(30)]
    split = energy.split_charge([(T0, T0 + timedelta(minutes=30), 6.0)], T0, T0 + timedelta(minutes=30), night)
    assert split["grid"] == pytest.approx(3.0) and split["solar"] == 0 and len(split["grid_by_minute"]) == 30


def test_a_gap_in_the_readings_lowers_coverage_and_five_minute_rows_cover_their_five():
    half = [row(i, solar=9000, home=8000, grid=-1000) for i in range(0, 15, 5)]
    split = energy.split_charge([(T0, T0 + timedelta(minutes=30), 6.0)], T0, T0 + timedelta(minutes=30), half)
    assert split["covered"] == pytest.approx(0.5)
    every_five = [row(i, solar=9000, home=8000, grid=-1000) for i in range(0, 30, 5)]
    split = energy.split_charge([(T0, T0 + timedelta(minutes=30), 6.0)], T0, T0 + timedelta(minutes=30), every_five)
    assert split["covered"] == pytest.approx(1.0) and split["solar"] == pytest.approx(3.0)


# ---- the Powerwall's own mix and prices


@pytest.fixture
def plan():
    cost.use_plan(
        cost.compile_rows(
            "Springfield",
            [{"from": "2026-01-01", "off_peak": 0.10, "part_peak": 0.20, "peak": 0.40}],
            [{"months": None, "days": "all", "start": 0, "end": 24, "period": "peak"}],
        )
    )
    yield
    cost.use_plan(None)


def test_the_batterys_solar_share_is_what_it_took_from_the_sun_over_what_it_took(plan):
    hours = [
        {"hour": datetime(2026, 7, 10, 12), "from_sun_kwh": 6.0, "in_kwh": 6.0},
        {"hour": datetime(2026, 7, 10, 23), "from_sun_kwh": 0.0, "in_kwh": 4.0},
    ]
    mix = energy.battery_mix(hours)
    assert mix["sun_frac"] == pytest.approx(0.6) and mix["in_kwh"] == 10 and mix["grid_rate"] == pytest.approx(0.40)
    empty = energy.battery_mix([])
    assert empty["sun_frac"] is None and empty["grid_rate"] is None
    assert energy.battery_mix([{"hour": datetime(2026, 7, 10, 12), "from_sun_kwh": 5.0, "in_kwh": 5.0}])["grid_rate"] is None, "no grid part: nothing to price"


def test_costs_count_the_sun_as_free_and_price_the_grid_part_of_the_battery(plan):
    split = {"solar": 2.0, "battery": 4.0, "grid": 1.0, "total": 7.0, "covered": 1.0, "grid_by_minute": [(T0, 0.5), (T0 + timedelta(minutes=1), 0.5)]}
    mix = {"sun_frac": 0.75, "grid_rate": 0.30, "in_kwh": 10.0}
    got = energy.provenance(split, mix, PT, 7.0)
    assert got["status"] == "ok" and got["solar_kwh"] == 2.0 and got["battery_kwh"] == 4.0 and got["grid_kwh"] == 1.0
    assert got["grid_cost"] == pytest.approx(0.40) and got["battery_cost"] == pytest.approx(4.0 * 0.25 * 0.30)
    assert got["battery_sun_frac"] == 0.75 and got["approx"] is False


def test_the_split_is_scaled_to_the_meters_kwh_so_the_parts_add_up(plan):
    split = {"solar": 2.0, "battery": 0.0, "grid": 2.0, "total": 4.0, "covered": 1.0, "grid_by_minute": [(T0, 2.0)]}
    got = energy.provenance(split, {"sun_frac": None, "grid_rate": None, "in_kwh": 0.0}, PT, 5.0)
    assert got["solar_kwh"] + got["battery_kwh"] + got["grid_kwh"] == pytest.approx(5.0) and got["grid_cost"] == pytest.approx(2.5 * 0.40)
    assert got["approx"], "no battery history to read the Powerwall's mix from"


def test_a_charge_with_too_little_behind_it_is_marked_none_and_no_plan_means_no_cost():
    thin = {"solar": 1.0, "battery": 0.0, "grid": 0.0, "total": 4.0, "covered": 0.25, "grid_by_minute": []}
    assert energy.provenance(thin, {"sun_frac": None, "grid_rate": None, "in_kwh": 0}, PT, 4.0) == {"status": "none", "coverage": 0.25}
    cost.use_plan(None)
    split = {"solar": 1.0, "battery": 1.0, "grid": 1.0, "total": 3.0, "covered": 1.0, "grid_by_minute": [(T0, 1.0)]}
    got = energy.provenance(split, {"sun_frac": 0.5, "grid_rate": None, "in_kwh": 2.0}, PT, 3.0)
    assert got["grid_cost"] is None and got["battery_cost"] is None and not got["approx"], "five-minute readings are the resolution there is: not a reason to mark it"


def test_rows_a_local_gateway_wrote_go_with_its_other_leftovers_and_a_clean_install_is_untouched():
    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(" ".join(sql.split()))

    conn, state = Conn(), State()
    energy.drop_gateway(state.get, state.put, lambda k: state.pop(k), conn)
    assert conn.sql == ["DELETE FROM cc_energy WHERE source = 'local'"], "idempotent: harmless where there are none"
    energy.drop_gateway(state.get, state.put, lambda k: state.pop(k))  # without a connection it still does the rest


def test_every_stored_split_is_redone_once_when_the_model_changes():
    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(" ".join(sql.split()))

        def commit(self):
            self.sql.append("COMMIT")

    state, conn = State(), Conn()
    assert energy.reset_sources(state.get, state.put, conn) is True
    assert conn.sql == ["DELETE FROM cc_charge_sources", "COMMIT"] and state[energy.MODEL_KEY] == str(energy.MODEL)
    assert energy.reset_sources(state.get, state.put, conn) is False and len(conn.sql) == 2, "once"
    assert energy.MODEL >= 2


class HourConn:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def execute(self, sql, params=()):
        self.asked.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


def test_hours_are_utc_clock_hours_with_how_much_of_each_the_readings_cover():
    lo, hi = datetime(2026, 11, 1, 7, tzinfo=timezone.utc), datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
    base = {"solar_kwh": 0.0, "home_kwh": 1.23456, "grid_in_kwh": 1.2, "grid_out_kwh": -0.0, "battery_in_kwh": 0.0, "battery_out_kwh": 0.0}
    # 08:00 and 09:00 UTC are both 01:00 in Pacific on the night the clocks fall back: two hours, not one
    conn = HourConn([{"hour_utc": datetime(2026, 11, 1, 8), **base, "rows": 12}, {"hour_utc": datetime(2026, 11, 1, 9), **base, "rows": 6}])
    got = energy.hours(conn, lo, hi)
    assert [h["hour"] for h in got] == [datetime(2026, 11, 1, 8, tzinfo=timezone.utc), datetime(2026, 11, 1, 9, tzinfo=timezone.utc)]
    assert got[0]["home_kwh"] == 1.235 and str(got[0]["grid_out_kwh"]) == "0.0" and got[0]["coverage"] == 1.0 and got[1]["coverage"] == 0.5
    assert conn.asked[0][1] == ("home", lo, hi) and "GROUP BY 1" in energy.HOURS_SQL and "AT TIME ZONE 'UTC'" in energy.HOURS_SQL
    assert energy.hours(HourConn([]), lo, hi) == []

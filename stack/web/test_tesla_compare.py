"""The stream beside the car's writer: the numbers, the per-drive match, the gap evidence,
and the background job that keeps the page from ever doing the work."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import tesla_compare as tc
from test_tesla_stream import ALPHA, T0, connectivity, payload

TZ = ZoneInfo("America/Chicago")
M = timedelta(minutes=1)


def drive(start, miles=5.0, odo=12000.0, rng=210.0):
    """A drive of `miles` from minute `start`, a record each minute, parked at the end."""
    p = [connectivity(start - 2, "CONNECTED"), payload(start - 2, Gear="ShiftStateP", Odometer=odo, RatedRange=rng, BatteryLevel=70.0, Location=(39.78, -89.65))]
    p.append(payload(start, Gear="ShiftStateD"))
    steps = 10
    for i in range(1, steps + 1):
        p.append(payload(start + i, VehicleSpeed=30.0, Odometer=round(odo + miles * i / steps, 3), RatedRange=round(rng - 1.2 * miles * i / steps, 2), Location=(39.78 + 0.001 * i, -89.65)))
    p.append(payload(start + steps + 0.5, VehicleSpeed=0.0, Gear="ShiftStateP"))
    p.append(connectivity(start + steps + 20, "DISCONNECTED"))
    return p


def charge(start, minutes=60, kw=7.2):
    p = [connectivity(start - 1, "CONNECTED")]
    p += [payload(start + m, DetailedChargeState="DetailedChargeStateCharging", ACChargingPower=kw, ACChargingEnergyIn=round(0.1 + m * kw / 60, 3)) for m in range(minutes)]
    p.append(payload(start + minutes, DetailedChargeState="DetailedChargeStateComplete", ACChargingPower=0.0, ACChargingEnergyIn=round(0.1 + minutes * kw / 60, 3)))
    return p + [connectivity(start + minutes + 15, "DISCONNECTED")]


def a_day():
    return drive(60, miles=5.0) + drive(240, miles=12.0, odo=12005.0, rng=204.0) + charge(400, 60) + drive(600, miles=3.0, odo=12017.0, rng=200.0)


HI = T0 + timedelta(hours=14)


# ---- the stream's side, worked out in memory
def test_the_stream_side_counts_drives_miles_charges_and_sleep():
    out = tc.stream_out(a_day(), HI)
    s = tc.side(out["trips"], out["charging_sessions"], out["drive_ranges"], out["states"], 0.25, T0, HI)
    assert s["totals"]["drives"] == 3 and s["totals"]["miles"] == 20.0
    assert s["totals"]["charges"] == 1 and abs(s["totals"]["kwh_added"] - 7.2) < 0.2
    # rated range fell 1.2 per mile: 24 rated miles at 0.25 kWh each
    assert abs(s["totals"]["kwh_used"] - 6.0) < 0.1
    # asleep from 15 minutes after each goodbye to the next record, as the OFFLINE stretches hold it (a stretch ends at its last sample)
    assert 8 < s["totals"]["asleep_h"] < 9.5


def test_range_at_reads_the_stretch_holding_the_moment_or_the_last_before():
    runs = [(T0, T0 + 10 * M, 200.0), (T0 + 11 * M, T0 + 20 * M, 190.0)]
    assert tc.range_at(runs, T0 + 5 * M) == 200.0
    assert tc.range_at(runs, T0 + 10 * M + timedelta(seconds=30)) == 200.0
    assert tc.range_at(runs, T0 + 30 * M) == 190.0 and tc.range_at(runs, T0 - M) is None


def test_with_no_measured_kwh_per_rated_mile_kwh_used_is_left_out_not_guessed():
    out = tc.stream_out(a_day(), HI)
    assert tc.side(out["trips"], out["charging_sessions"], out["drive_ranges"], out["states"], None, T0, HI)["totals"]["kwh_used"] == 0


# ---- the match table
def row(minutes, value, key="miles"):
    return {"start": T0 + minutes * M, "end": T0 + (minutes + 10) * M, key: value}


def test_drives_pair_by_overlap_and_disagreements_are_marked():
    stream = [row(60, 5.0), row(240, 12.0), row(600, 3.0), row(700, 1.0)]
    stored = [row(61, 5.0), row(243, 10.0), row(615, 3.0)]
    got = tc.match(stream, stored, "miles", tc.MILES_OFF)
    assert [(p["stream"] is not None, p["stored"] is not None, p["off"]) for p in got] == [
        (True, True, False),
        (True, True, True),  # 12 vs 10 miles
        (True, False, True),  # 600-610 and 615-625 do not overlap: not the same drive
        (False, True, True),
        (True, False, True),  # the stream saw a drive the writer did not
    ]
    assert got[0]["dt_s"] == -60


def test_a_drive_split_differently_is_one_difference_not_two():
    """The stream had one drive where the writer had two (a stop in park): one row, off once."""
    stream = [{"start": T0, "end": T0 + 35 * M, "miles": 26.0}]
    stored = [{"start": T0, "end": T0 + M, "miles": 0.0}, {"start": T0 + 3 * M, "end": T0 + 35 * M, "miles": 25.8}]
    got = tc.match(stream, stored, "miles", tc.MILES_OFF)
    assert len(got) == 1 and got[0]["off"] and got[0]["stored"]["n"] == 2 and got[0]["stored"]["miles"] == 25.8


def test_a_charge_split_at_a_brief_unplug_is_the_same_charge():
    stream = [{"start": T0, "end": T0 + 300 * M, "kwh": 37.5, "type": "AC"}]
    stored = [{"start": T0 + M, "end": T0 + 3 * M, "kwh": 0.14, "type": "AC"}, {"start": T0 + 8 * M, "end": T0 + 301 * M, "kwh": 37.2, "type": "AC"}]
    got = tc.match(stream, stored, "kwh", tc.KWH_OFF, joins_splits=True)
    assert len(got) == 1 and not got[0]["off"]


def test_a_drive_still_under_way_at_the_end_of_the_window_is_not_a_difference():
    hi = T0 + 60 * M
    stream = [{"start": T0 + 30 * M, "end": hi, "miles": 26.6}]
    stored = [{"start": T0 + 30 * M, "end": T0 + 53 * M, "miles": 18.9}]
    got = tc.match(stream, stored, "miles", tc.MILES_OFF, hi=hi)
    assert len(got) == 1 and got[0]["under_way"] and not got[0]["off"]


def test_a_crumb_on_one_side_alone_is_not_a_difference():
    """A garage shuffle the writer's minute polls missed, a charge of 0.09 kWh it read as none."""
    got = tc.match([row(0, 0.1)], [], "miles", tc.MILES_OFF)
    assert not got[0]["off"]
    got = tc.match([row(0, 0.09, "kwh")], [row(0, None, "kwh")], "kwh", tc.KWH_OFF)
    assert not got[0]["off"]


def test_small_differences_are_not_marked():
    assert not tc.match([row(0, 100.0)], [row(1, 102.5)], "miles", tc.MILES_OFF)[0]["off"], "2.5 % of a long drive"
    assert not tc.match([row(0, 1.0)], [row(1, 1.4)], "miles", tc.MILES_OFF)[0]["off"], "0.4 mi of a short one"


# ---- the gap evidence
def test_resent_records_goodbyes_and_unannounced_silences_are_counted_per_day():
    recs = drive(60)  # said goodbye at 80: a sleep, not a silence
    # the receiver goes away mid-drive after the record at 303: nothing until the car reconnects at 345 and resends
    recs += [connectivity(298, "CONNECTED"), payload(299, Gear="ShiftStateD"), payload(300, VehicleSpeed=30.0), payload(303, VehicleSpeed=31.0)]
    recs += [connectivity(345, "CONNECTED"), payload(345, resend=True, VehicleSpeed=0.0, Gear="ShiftStateP"), payload(345.1, resend=True, Odometer=12010.0), connectivity(360, "DISCONNECTED")]
    got = tc.gaps(recs, T0, HI, TZ)
    assert len(got) == 1
    d = got[0]
    assert d["resent"] == 2 and d["disconnects"] == 2 and d["reconnects"] == 3
    assert d["silences"] == 1, "one silence nobody announced; the sleeps were said"
    assert d["longest_silence_min"] == 42.0, "from the last record at 303 to the reconnect at 345"


def test_a_silence_still_running_at_the_end_of_the_window_counts_to_the_end():
    recs = [connectivity(0, "CONNECTED"), payload(1, Gear="ShiftStateD"), payload(2, VehicleSpeed=30.0)]
    d = tc.gaps(recs, T0, T0 + 60 * M, TZ)[0]
    assert d["silences"] == 1 and d["longest_silence_min"] == 58.0


def test_gaps_split_by_local_day():
    late = [payload(m, BatteryLevel=60.0) for m in (13 * 60, 15 * 60)]  # 23:00 and 01:00 in Springfield
    assert [d["records"] for d in tc.gaps(late, T0, T0 + timedelta(days=1), TZ)] == [1, 1]


# ---- the whole comparison, from the database
class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, raw, trips=(), charges=(), ranges=(), states=(), k=0.25):
        self.raw, self.trips, self.charges, self.ranges, self.states, self.k = raw, list(trips), list(charges), list(ranges), list(states), k
        self.sql = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s)
        if s.startswith("SELECT MIN(at) FROM cc_tesla_raw"):
            return Result([(T0 - timedelta(days=1),)])
        if s.startswith("SELECT record FROM cc_tesla_raw"):
            return Result([(p,) for p in self.raw])
        if s.startswith("SELECT kwh_per_rated_mi"):
            return Result([(self.k,)])
        if "FROM trips" in s:
            return Result(self.trips)
        if "FROM charging_sessions" in s:
            return Result(self.charges)
        if "FROM drive_ranges" in s:
            return Result(self.ranges)
        if "FROM states" in s:
            return Result(self.states)
        return Result()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_comparison_sets_the_stream_beside_what_the_writer_stored():
    # the writer has the first two drives (a minute late, the second 0.6 mi short) and the charge a little light; no third drive
    trips = [(T0 + 61 * M, T0 + 71 * M, 12000.0, 12005.0), (T0 + 241 * M, T0 + 251 * M, 12005.0, 12016.4)]
    charges = [(T0 + 401 * M, T0 + 460 * M, 7.0, "AC")]
    ranges = [(T0, T0 + 61 * M, 210.0), (T0 + 71 * M, T0 + 241 * M, 204.0), (T0 + 251 * M, T0 + 14 * 60 * M, 190.32)]
    states = [(T0, T0 + 50 * M, "OFFLINE")]
    got = tc.compare(Conn(a_day(), trips, charges, ranges, states), ALPHA, HI, 7, TZ)
    assert got["lo"] == T0 - timedelta(days=1), "the window starts at the stream's first record"
    assert got["stream"]["drives"] == 3 and got["stored"]["drives"] == 2
    assert got["stored"]["miles"] == 16.4 and got["stored"]["kwh_used"] == round((210 - 204) * 0.25 + (204 - 190.32) * 0.25, 1)
    assert got["stored"]["kwh_added"] == 7.0 and got["stored"]["asleep_h"] == round(50 / 60, 2)
    assert [p["off"] for p in got["drives"]] == [False, True, True]
    assert [p["off"] for p in got["charges"]] == [False]
    assert got["gaps"] and got["computed_at"] == HI


def test_no_stream_records_no_comparison():
    class Empty(Conn):
        def execute(self, sql, params=None):
            return Result([(None,)]) if "MIN(at)" in sql else Result()

    assert tc.compare(Empty([]), ALPHA, HI, 7, TZ) is None


def test_the_window_is_capped_and_never_before_the_first_record():
    assert tc.window(HI, 400, HI - timedelta(days=100)) == (HI - timedelta(days=tc.MAX_DAYS), HI)
    assert tc.window(HI, 0, HI - timedelta(days=100)) == (HI - timedelta(days=1), HI)
    assert tc.window(HI, 7, HI - timedelta(hours=3)) == (HI - timedelta(hours=3), HI)
    assert tc.window(HI, 7, None) is None


# ---- the background job
def test_the_page_reads_what_is_kept_and_one_job_runs_per_car_and_window():
    jobs, clock = [], [HI]
    c = tc.Comparer(connect=lambda: Conn(a_day()), tz=TZ, clock=lambda: clock[0], spawn=jobs.append)
    first = c.get(ALPHA, 7)
    assert first["result"] is None and first["running"] and len(jobs) == 1
    assert c.get(ALPHA, 7)["running"] and len(jobs) == 1, "asked again while it runs: no second job"
    jobs.pop()()
    got = c.get(ALPHA, 7)
    assert got["result"]["stream"]["drives"] == 3 and not got["running"] and not jobs
    clock[0] = HI + timedelta(seconds=tc.TTL_S)
    stale = c.get(ALPHA, 7)
    assert stale["result"] is not None and stale["running"] and len(jobs) == 1, "a stale result shows while a fresh one is worked out"


def test_a_failed_comparison_is_said_not_raised():
    def broken():
        raise ConnectionError("database is away")

    jobs = []
    c = tc.Comparer(connect=broken, tz=TZ, clock=lambda: HI, spawn=jobs.append)
    c.get(ALPHA)
    jobs.pop()()
    got = c.get(ALPHA, start=False)
    assert got["result"] is None and "database is away" in got["error"]

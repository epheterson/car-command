"""The records board: each record is the drive or charge that set it, ties go to the first,
and the efficiency pair counts only drives long enough for Wh per mile to mean something."""

from datetime import datetime, timedelta, timezone

import records

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def drive(i, miles, minutes=20, a=80.0, b=70.0, mph=50.0, temp=60.0, hi=180.0, lo=170.0, kwh=None, days=0):
    return {
        "id": i,
        "vin": "V1",
        "start_date": T0 + timedelta(days=days or i),
        "miles": miles,
        "duration": timedelta(minutes=minutes),
        "level_start": a,
        "level_end": b,
        "max_mph": mph,
        "temp_f": temp,
        "elev_max_m": hi,
        "elev_min_m": lo,
        "kwh_used": kwh if kwh is not None else miles * 0.27,
    }


def test_every_record_names_the_drive_that_set_it():
    ds = [
        drive(1, 4.0, minutes=9, temp=31.0, kwh=1.6),
        drive(2, 38.2, minutes=70, a=86, b=61, mph=76, hi=210, kwh=12.3),
        drive(3, 12.2, minutes=26, temp=88.0, lo=151, kwh=3.0),
        drive(4, 11.0, minutes=25, kwh=3.6),
    ]
    got = {b["key"]: (b["row"]["id"], round(b["value"], 1)) for b in records.board(ds)}
    assert got["distance"] == (2, 38.2) and got["battery"] == (2, 25.0) and got["time"] == (2, round(70 / 60, 1)) and got["speed"] == (2, 76.0)
    assert got["cold"] == (1, 31.0) and got["hot"] == (3, 88.0)
    assert got["high"] == (2, round(210 * 3.28084, 1)) and got["low"] == (3, round(151 * 3.28084, 1)), "metres stored, feet out (the elev filter converts)"
    assert got["best_wh"] == (3, round(3.0 * 1000 / 12.2, 1)) and got["worst_wh"] == (4, round(3.6 * 1000 / 11.0, 1))
    assert [b["key"] for b in records.board(ds)][:4] == ["distance", "battery", "time", "speed"], "the board's order"


def test_a_tie_goes_to_the_drive_that_set_it_first():
    ds = [drive(5, 20.0, days=9), drive(6, 20.0, days=2)]
    assert next(b for b in records.board(ds) if b["key"] == "distance")["row"]["id"] == 6


def test_records_with_nothing_behind_them_are_left_off():
    ds = [dict(drive(1, 3.0), elev_max_m=None, elev_min_m=None, temp_f=None, max_mph=None, level_start=None, level_end=None)]
    keys = {b["key"] for b in records.board(ds)}
    assert keys == {"distance", "time"}, "no elevation, temperature, speed, battery or efficiency to show: none shown as zero"
    assert records.board([]) == []


def test_efficiency_steps_down_once_when_few_drives_are_long_and_says_so():
    long = [drive(i, 12.0) for i in range(1, 4)]
    assert records.efficiency_floor(long) == records.EFF_MIN_MI
    short = [drive(i, 6.0) for i in range(1, 4)] + [drive(9, 12.0)]
    assert records.efficiency_floor(short) == records.EFF_FALLBACK_MI
    assert records.efficiency_floor([drive(i, 3.0) for i in range(1, 9)]) is None, "short hops only: no efficiency record at all"
    best = next(b for b in records.board(short) if b["key"] == "best_wh")
    assert best["note"] == records.EFF_FALLBACK_MI, "the record says how long a drive had to be"


def test_the_fastest_charge_is_its_own_row():
    row = {"id": 77, "session_start_date": T0}
    b = records.board([drive(1, 5.0)], (row, 150.0))
    fast = next(x for x in b if x["key"] == "peak_kw")
    assert fast["kind"] == "charge" and fast["row"]["id"] == 77 and fast["value"] == 150.0
    assert "peak_kw" not in {x["key"] for x in records.board([drive(1, 5.0)], None)}


def test_the_sensors_minus_forty_is_no_reading_not_a_record():
    ds = [drive(1, 5.0, temp=-40.0), drive(2, 6.0, temp=12.0)]
    cold = next(b for b in records.board(ds) if b["key"] == "cold")
    assert cold["row"]["id"] == 2 and cold["value"] == 12.0


def test_a_drive_holding_several_records_is_shown_once_with_all_of_them():
    ds = [drive(1, 38.2, minutes=70, mph=76, b=12.0), drive(2, 4.0, minutes=9), drive(3, 12.0, kwh=2.5), drive(4, 11.0, kwh=3.9)]
    row = {"id": 9, "session_start_date": T0}
    groups = records.fold(records.board(ds, (row, 150.0), (row, 60.0)))
    keys = [sorted(g["keys"]) for g in groups]
    assert [g["lead"] for g in groups].count(True) == 2, "the longest drive is also the fastest: two leads, not three cards of one drive"
    assert groups[0]["titles"][0]["key"] == "distance" and {"speed", "time", "lowest"} <= groups[0]["keys"]
    assert groups[1]["kind"] == "charge" and sorted(groups[1]["keys"]) == ["most_kwh", "peak_kw"], "one charge holding both charge records"
    assert len(keys) == len({(g["kind"], g["row"]["id"]) for g in groups}), "each drive or charge once"


def test_the_lowest_battery_is_the_lowest_level_a_drive_ended_at():
    ds = [drive(1, 5.0, b=31.0), drive(2, 6.0, b=8.0), drive(3, 7.0, b=None)]
    low = next(b for b in records.board(ds) if b["key"] == "lowest")
    assert low["row"]["id"] == 2 and low["value"] == 8.0


def test_a_drive_using_less_than_its_miles_allow_is_not_most_efficient():
    """A drive whose end level was read twenty minutes in: 115 miles on 9 % of the pack."""
    ds = [drive(i, 20.0, kwh=5.4) for i in range(1, 6)] + [drive(9, 114.6, kwh=6.6)]
    best = next(b for b in records.board(ds) if b["key"] == "best_wh")
    assert best["row"]["id"] != 9 and round(best["value"]) == 270
    assert next(b for b in records.board(ds) if b["key"] == "distance")["row"]["id"] == 9, "still the longest drive"


def test_the_plausible_floor_is_each_cars_own():
    bravo = [dict(drive(i, 20.0, kwh=3.0), vin="V2") for i in range(10, 13)]  # a frugal car at 150 Wh/mi
    alpha = [drive(i, 20.0, kwh=7.0) for i in range(1, 4)]  # 350 Wh/mi
    assert len(records.plausible(alpha + bravo)) == 6, "Bravo's drives are not judged against Alpha's median"

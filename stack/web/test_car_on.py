"""The car on and parked, no drive (cc_car_on): its own colour in the strips and lanes, its own piece of a
parked stretch, a phrase on the parked rows, and awake parked time in the sleep and phantom-drain figures."""

from datetime import datetime, timedelta, timezone

from chartdata import lane
from day import day_sessions, day_totals, split_parked
from daypage import gap_text
from parkedview import figures
from stats import timeline_days
from units import days_between, ribbon_segments

T = lambda h, m=0: datetime(2026, 10, 22, h, m, tzinfo=timezone.utc)
ON = [(T(5, 38), T(6, 27), "V1")]  # trip 131074's stretch, the car on and parked 49 min


class U:
    labels = {"dist": "mi", "econ": "Wh/mi", "temp": "°F"}

    def distance(self, x):
        return x

    def temperature(self, x):
        return x


def test_the_strip_and_the_lane_show_the_car_on_as_its_own_colour_not_a_drive():
    states = [(T(0), T(5, 38), "PARKED"), (T(5, 38), T(6, 25), "DRIVING"), (T(6, 25), T(6, 27), "IGNITION_ON"), (T(6, 27), T(9), "PARKED")]
    plain = ribbon_segments(states, [], T(4), T(8))
    assert "driving" in [s["label"] for s in plain], "without the stretch the plugin's DRIVING state reads as a drive"
    segs = ribbon_segments(states, [], T(4), T(8), ON)
    assert [s["label"] for s in segs] == ["idle", "caron", "idle"] and "driving" not in [s["label"] for s in segs]
    assert lane(segs)[1][2] == "caron"
    charged = ribbon_segments(states, [(T(6), T(6, 10), "CHARGING")], T(4), T(8), ON)
    assert [s["label"] for s in charged if s["start"] <= T(6, 5) < s["end"]] == ["charging"], "a charge still beats it"


def test_the_day_cuts_a_parked_stretch_around_the_car_on_and_counts_it_as_awake():
    states = [(T(0), T(5, 38), "PARKED"), (T(5, 38), T(6, 27), "DRIVING"), (T(6, 27), T(9), "PARKED")]
    assert [(a, b, x) for a, b, x in split_parked(states, T(4), T(8), ON)] == [(T(4), T(5, 38), "idle"), (T(5, 38), T(6, 27), "caron"), (T(6, 27), T(8), "idle")]
    rows = day_sessions([], [], states, [(T(0), 80.0), (T(8), 79.0)], T(4), T(8), car_on=ON)
    on = next(r for r in rows if r["sub"] == "caron")
    assert on["title"] == "Car on" and round(on["on_h"] * 60) == 49 and on["asleep_share"] == 0.0
    tot = day_totals(rows, 75.0)
    assert tot["drives"] == 0 and tot["miles"] == 0 and round(tot["on_h"] * 60) == 49
    assert tot["asleep_h"] == 0.0 and tot["parked_h"] == 4.0, "awake parked time: no sleep, all of it parked"


def test_a_parked_row_says_how_long_the_car_was_on():
    row = {"kind": "parked", "sub": "caron", "duration": timedelta(minutes=49), "on_h": 49 / 60}
    assert gap_text(row, "Home", U()) == "Parked 49 min at Home · car on 49 min"
    assert "car on" not in gap_text(dict(row, on_h=0.01), "Home", U()) and "car on" not in gap_text(dict(row, on_h=0), "Home", U())


def test_the_parked_page_figures_carry_the_car_on_time():
    f = figures(T(5), T(8), [(T(5), 80.0)], [], [], [], [(T(5), T(8), "PARKED")], [], None, None, ON)
    assert round(f["on_h"] * 60) == 49 and f["on"] == timedelta(hours=f["on_h"])
    assert figures(T(5), T(8), [], [], [], [], [], [], None, None)["on_h"] == 0


def test_the_sleep_hours_count_it_as_idle_awake_not_driving():
    states = [(T(0), T(5, 38), "OFFLINE"), (T(5, 38), T(6, 27), "DRIVING"), (T(6, 27), T(10), "PARKED")]
    day = datetime(2026, 10, 22).date()
    now = T(12)
    on = days_between(states, [], day, day, timezone.utc, now, ON)[0]
    off = days_between(states, [], day, day, timezone.utc, now)[0]
    assert round(on["hours"]["driving"], 2) == 0 and round(on["hours"]["idle"], 2) == round(off["hours"]["idle"] + 49 / 60, 2)
    assert off["hours"]["driving"] > 0.8 and on["hours"]["sleeping"] == off["hours"]["sleeping"]


def test_the_timeline_parked_row_carries_the_car_on_time():
    drives = [
        {
            "id": 1,
            "vin": "V1",
            "car_name": "Alpha",
            "start_date": T(1),
            "destination_date": T(1, 10),
            "from": "Home",
            "to": "Work",
            "miles": 3.0,
            "duration": timedelta(minutes=10),
            "level_start": 70,
            "level_end": 69,
        },
        {
            "id": 2,
            "vin": "V1",
            "car_name": "Alpha",
            "start_date": T(9),
            "destination_date": T(9, 10),
            "from": "Work",
            "to": "Home",
            "miles": 3.0,
            "duration": timedelta(minutes=10),
            "level_start": 69,
            "level_end": 68,
        },
    ]
    days = timeline_days(drives, [], [], timezone.utc, car_on=ON)
    parked = next(e for d in days for e in d["events"] if e["kind"] == "parked")
    assert round(parked["on_h"] * 60) == 49
    other = timeline_days(drives, [], [], timezone.utc, car_on=[(a, b, "V2") for a, b, _ in ON])
    assert next(e for d in other for e in d["events"] if e["kind"] == "parked")["on_h"] == 0, "another car's time is not this car's"

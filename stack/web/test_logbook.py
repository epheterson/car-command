import csv
import io
from datetime import datetime, timezone, timedelta

import logbook
from units import Units

TZ = timezone(timedelta(hours=-5))


def drive(i, tag=None, miles=10.0, named=True, **kw):
    t0 = datetime(2026, 10, i, 14, 5, tzinfo=timezone.utc)
    d = {
        "id": i,
        "start_date": t0,
        "destination_date": t0 + timedelta(minutes=30),
        "car_name": "Alpha",
        "tag": tag,
        "start_odometer": 1000.0 + i,
        "destination_odometer": 1000.0 + i + (miles or 0),
        "miles": miles,
        "from": "Alpha Depot" if named else "Elm Street, Springfield",
        "from_named": named,
        "to": "Bravo Office",
        "to_named": True,
        "start_road": "Elm Street",
        "start_city": "Springfield",
        "dest_road": "Oak Avenue",
        "dest_city": "Springfield",
        "start_position_latitude": 40.0,
        "start_position_longitude": -80.0,
        "destination_position_latitude": 40.1,
        "destination_position_longitude": -80.1,
    }
    return {**d, **kw}


def parse(text):
    return list(csv.reader(io.StringIO(text)))


def test_select_keeps_finished_drives_oldest_first_and_filters_by_tag():
    ds = [drive(3, "Work"), drive(1, "Personal"), drive(2, None), drive(4, "Work", destination_date=None)]
    assert [d["id"] for d in logbook.select(ds, None)] == [1, 2, 3]
    assert [d["id"] for d in logbook.select(ds, ["Work"])] == [3]
    assert [d["id"] for d in logbook.select(ds, ["Work", "Personal"])] == [1, 3]
    assert [d["id"] for d in logbook.select(ds, [""])] == [1, 2, 3]


def test_a_row_has_local_times_odometers_distance_and_place_names_only():
    out = parse(logbook.build([drive(1, "Work", miles=12.34)], Units(False), TZ))
    assert out[0] == ["Date", "Start time", "End time", "Car", "Tag", "Start odometer (mi)", "End odometer (mi)", "Distance (mi)", "From", "To"]
    assert out[1] == ["2026-10-01", "09:05", "09:35", "Alpha", "Work", "1001.0", "1013.3", "12.3", "Alpha Depot", "Bravo Office"]
    assert "Elm Street" not in "".join(out[1])


def test_addresses_are_added_only_when_asked_and_unnamed_places_stay_blank_without_them():
    d = drive(1, named=False)
    plain = parse(logbook.build([d], Units(False), TZ))[1]
    assert plain[8] == "Springfield" and len(plain) == 10
    full = parse(logbook.build([d], Units(False), TZ, addresses=True))
    assert full[0][-2:] == ["From address", "To address"]
    assert full[1][-2:] == ["Elm Street, Springfield", "Oak Avenue, Springfield"]


def test_metric_units_convert_odometer_and_distance():
    out = parse(logbook.build([drive(1, miles=10.0)], Units(True), TZ))
    assert out[0][5:8] == ["Start odometer (km)", "End odometer (km)", "Distance (km)"]
    assert out[1][5:8] == ["1611.0", "1627.0", "16.1"]


def test_a_missing_odometer_leaves_blank_cells():
    out = parse(logbook.build([drive(1, start_odometer=None, miles=None)], Units(False), TZ))
    assert out[1][5] == "" and out[1][7] == ""


def test_spreadsheet_formulas_are_quoted():
    out = parse(logbook.build([drive(1, tag="=SUM(A1)", car_name="+Alpha")], Units(False), TZ))
    assert out[1][4] == "'=SUM(A1)" and out[1][3] == "'+Alpha"


def test_totals_per_tag_and_overall_follow_a_blank_line():
    ds = [drive(1, "Work", 10), drive(2, "Work", 5), drive(3, "Personal", 2), drive(4, None, 1)]
    out = parse(logbook.build(ds, Units(False), TZ, with_totals=True))
    assert out[5] == []
    tail = [(r[0], r[4], r[7], r[8]) for r in out[6:]]
    assert tail == [("Total", "Work", "15.0", "2 drives"), ("Total", "Personal", "2.0", "1 drive"), ("Total", "(untagged)", "1.0", "1 drive"), ("Total", "All", "18.0", "4 drives")]


def test_one_tag_has_no_all_row_and_no_drives_has_no_totals():
    out = parse(logbook.build([drive(1, "Work")], Units(False), TZ, with_totals=True))
    assert [r[4] for r in out[3:]] == ["Work"]
    assert len(parse(logbook.build([], Units(False), TZ, with_totals=True))) == 1


def test_an_unnamed_end_falls_back_to_town_then_city_then_street_and_stays_blank_only_when_unknown():
    d = drive(1, named=False, start_town="Springfield, IL")
    assert parse(logbook.build([d], Units(False), TZ))[1][8] == "Springfield, IL"
    assert parse(logbook.build([d], Units(False), TZ, addresses=True))[1][8] == "Elm Street, Springfield"
    cell = parse(logbook.build([dict(d, start_road=None, start_city=None, start_street="Pine Road, Shelbyville")], Units(False), TZ, addresses=True))[1]
    assert cell[8] == "Pine Road, Shelbyville" and cell[-2] == "Pine Road, Shelbyville"
    nothing = dict(d, start_town=None, start_road=None, start_city=None)
    assert parse(logbook.build([nothing], Units(False), TZ))[1][8] == ""

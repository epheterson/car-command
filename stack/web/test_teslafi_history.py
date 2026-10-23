"""TeslaFi's history summaries become the loader's rows."""

from datetime import date, datetime, timedelta, timezone

from teslafi_history import build, catchup_window, charge_from, trip_from

UTC = timezone.utc


def drive(**over):
    d = {
        "date": "2026-09-23 17:59:28",
        "dayDriveHours": 0,
        "dayDriveMinutes": 20,
        "startOdo": "95,408.96",
        "endOdo": "95,420.37",
        "miles": "11.41",
        "latitude": "39.80",
        "longitude": "-89.65",
        "endLatitude": "39.78",
        "endLongitude": "-89.64",
        "speedAVG": 36,
        "speedMAX": 84,
        "temperatureAVG": "71.40",
        "tag": "Commute",
        "noOutsideTemperatureReported": 0,
        "driveKwh": 3.42,
    }
    d.update(over)
    return d


def charge(**over):
    c = {
        "date": "2026-09-17 20:39:07",
        "hours": 2,
        "minutes": 14,
        "totalMinutes": 134,
        "startPercent": "70.50",
        "chargePercent": 90,
        "lastDataBatteryLevel": 90,
        "totalEnergyAdded": "12.92",
        "odometer": "62285.8734",
        "superChargerFlag": 0,
        "fastChargerType": "",
        "avgVoltage": "204.45",
        "avgCurrent": "29.56",
        "avgChargerPower": "0.00",
        "latitude": "39.80",
        "longitude": "-89.65",
        "fw": "2026.26.6.5",
    }
    c.update(over)
    return c


def test_a_drive_summary_is_a_trip_with_its_figures():
    t = trip_from(drive())
    assert t["start_date"] == datetime(2026, 9, 23, 17, 59, 28, tzinfo=UTC), "TeslaFi's date is the start, in UTC"
    assert t["destination_date"] == t["start_date"] + timedelta(minutes=20)
    assert t["start_odometer"] == 95408.96 and t["destination_odometer"] == 95420.37, "thousands separators are read"
    assert t["max_mph"] == 84 and t["temp_f"] == 71.4 and t["tag"] == "Commute"
    assert t["kwh"] == 3.42, "the summary's own energy: it has no battery line to work one out from"
    assert trip_from(drive(driveKwh=0))["kwh"] is None
    z = trip_from(drive(dayDriveMinutes=0, miles="0.01", temperatureAVG="0.00", noOutsideTemperatureReported=1, tag=""))
    assert z["destination_date"] == z["start_date"] + timedelta(minutes=1) and z["temp_f"] is None and z["tag"] is None
    assert trip_from({"date": "garbage"}) is None


def test_a_charge_summary_is_a_session():
    c = charge_from(charge())
    assert c["session_start_date"] == datetime(2026, 9, 17, 20, 39, 7, tzinfo=UTC) and c["session_end_date"] == c["session_start_date"] + timedelta(minutes=134)
    assert (c["start_level"], c["end_level"], c["real_charged"], c["charging_type"]) == (70.5, 90.0, 12.92, "AC")
    assert c["volts"] == 204.45 and c["amps"] == 29.56 and c["power_kw"] == 0.0 and c["software"] == "2026.26.6.5"
    assert charge_from(charge(superChargerFlag=1))["charging_type"] == "DC"
    assert charge_from(charge(superChargerFlag=0, fastChargerType="Tesla"))["charging_type"] == "DC"
    assert charge_from(charge(superChargerFlag=1))["network"] == "supercharger" and charge_from(charge(fastChargerType="MCSingleWireCAN"))["network"] is None
    assert charge_from(charge(fastChargerType="ACSingleWireCAN"))["charging_type"] == "AC", "the connector name is not a fast charger"
    assert charge_from(charge(fastChargerType="MCSingleWireCAN"))["charging_type"] == "DC"


def test_build_gives_the_writer_its_shape():
    out = build([drive(), drive(date="2026-09-23 22:51:28", dayDriveHours=1, dayDriveMinutes=18)], [charge()], "5YJ3E1EA0JF000001", "Alpha")
    assert out["first"] == datetime(2026, 9, 17, 20, 39, 7, tzinfo=UTC) and out["last"] == datetime(2026, 9, 24, 0, 9, 28, tzinfo=UTC)
    assert out["rows"] == 3 and out["vehicle"]["model"] == "Model 3"
    assert [s[2] for s in out["states"]] == ["PARKED", "DRIVING", "DRIVING"], "charges are parked, drives are driving, in time order"
    assert out["charging_states"][0][2] == "CHARGING" and len(out["drive_levels"]) == 2 and out["drive_levels"][0][2] == 70.5
    assert out["charging_electrics"] == [(out["charging_sessions"][0]["session_start_date"], out["charging_sessions"][0]["session_end_date"], (204.45, 29.56))]
    assert out["charging_powers"] == [], "a charger that reported no power adds no power row"
    assert out["software"][0][2] == "2026.26.6.5" and out["positions"] == [] and out["drive_ranges"] == []
    assert len(out["outside_temperatures"]) == 2
    assert build([], [], "V", None) is None


def test_catchup_window():
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    export_end = datetime(2026, 9, 4, 17, 20, tzinfo=UTC)
    feed_first = datetime(2026, 9, 28, 19, 15, tzinfo=UTC)
    assert catchup_window(export_end, feed_first, now) == (date(2026, 9, 4), date(2026, 9, 28)), "the hole between the export and the feed's first row"
    assert catchup_window(now - timedelta(hours=10), None, now) is None, "the feed's own window covers it"
    assert catchup_window(export_end, None, now) == (date(2026, 9, 4), date(2026, 9, 28)), "no feed row yet: up to now"
    assert catchup_window(None, feed_first, now) == (date(2025, 9, 28), date(2026, 9, 28)), "nothing at all: the last year"
    assert catchup_window(feed_first - timedelta(hours=2), feed_first, now) is None


def test_edges_reads_aliased_columns_from_dict_rows():
    from teslafi_history import edges

    class Conn:
        def execute(self, sql, params):
            self.params = params
            assert "AS before_feed" in sql and "AS feed_first" in sql, "unaliased, a dict row keeps one 'max' and the other is lost"
            return self

        def fetchone(self):
            assert self.params[:2] == ("teslafi-live:V1", "teslafi-history:V1"), "an earlier fill does not count as the hole's edge"
            return {"before_feed": datetime(2026, 9, 4, tzinfo=UTC), "feed_first": datetime(2026, 9, 28, tzinfo=UTC)}

    assert edges(Conn(), "V1") == (datetime(2026, 9, 4, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC))


def test_rows_that_share_a_start_second_collapse_to_one():
    same = charge()
    out = build([drive(), drive(dayDriveMinutes=5)], [same, dict(same), charge(totalMinutes=1, hours=0, minutes=1, date="2026-09-18 08:00:00")], "5YJ3E1EA0JF000001", "Alpha")
    assert len(out["trips"]) == 1 and len(out["charging_sessions"]) == 2, "an identical start is the same drive or charge listed twice"
    assert out["trips"][0]["destination_date"] - out["trips"][0]["start_date"] == timedelta(minutes=20), "the first listing wins"
    firsts = [a for a, _b, _v in out["drive_levels"]]
    assert len(firsts) == len(set(firsts)), "a one-minute charge's two level rows do not share a first_date"
    firsts = [a for a, _b, _v in out["states"]]
    assert len(firsts) == len(set(firsts))


def test_backfill_keeps_only_the_hole():
    from teslafi_history import backfill

    writes = []

    class Conn:
        def execute(self, sql, params=None):
            writes.append((sql, params))
            return self

        def fetchone(self):
            return None

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    calls = []

    def fetch(token, command, d0, d1):
        calls.append((command, d0, d1))
        return [drive(date="2026-09-04 10:00:00"), drive(date="2026-09-10 10:00:00"), drive(date="2026-09-28 20:00:00")] if command == "drives" else [charge(date="2026-09-17 20:39:07")]

    import teslafi_history as th

    seen = {}
    th_write = th.write_history
    th.write_history = lambda conn, vin, name, make, model, cap, out, iid, fn, tz, rolling=False: seen.update(out=out, iid=iid, rolling=rolling) or {
        "trips": len(out["trips"]),
        "charging_sessions": len(out["charging_sessions"]),
    }
    th.fill_after_import = lambda conn, vin, tz: None
    try:
        counts = backfill(
            lambda: Conn(), "V1", "tok", date(2026, 9, 4), date(2026, 9, 28), fetch=fetch, after=datetime(2026, 9, 4, 17, 20, tzinfo=UTC), before=datetime(2026, 9, 28, 19, 15, tzinfo=UTC)
        )
    finally:
        th.write_history = th_write
    assert [c[0] for c in calls] == ["drives", "charges"] and calls[0][1:] == (date(2026, 9, 4), date(2026, 9, 28))
    assert counts["trips"] == 1 and counts["charges_seen"] == 1 and counts["drives_seen"] == 3, "the drive before the export's end and the one after the feed began are left alone"
    assert seen["iid"] == "teslafi-history:V1" and seen["rolling"] is True

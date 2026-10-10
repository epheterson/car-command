"""Tested against the real 151-column export, not a guessed one."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import gzip
import io

from teslafi_import import iter_samples, model_from_vin, parse, replay, vehicle_of, vehicle_state

PT = ZoneInfo("America/Los_Angeles")
COLS = [
    "Date",
    "display_name",
    "vin",
    "car_type",
    "car_version",
    "odometer",
    "battery_level",
    "elevation",
    "inside_tempF",
    "sentry_mode",
    "is_climate_on",
    "fan_status",
    "charger_voltage",
    "charger_actual_current",
    "SelfDrivingMilesSinceReset",
    "battery_range",
    "outside_temp",
    "outside_tempF",
    "shift_state",
    "state",
    "charging_state",
    "charger_power",
    "charge_energy_added",
    "fast_charger_present",
    "fast_charger_type",
    "latitude",
    "longitude",
    "heading",
    "speed",
    "driveNumber",
    "chargeNumber",
    "idleNumber",
    "sleepNumber",
]


def row(date, **kw):
    d = {c: "" for c in COLS}
    d.update(
        {
            "Date": date,
            "display_name": "Bravo",
            "vin": "5YJ3E1EA0JF000001",
            "car_type": "models3",
            "car_version": "2018.32.2",
            "state": "online",
            "charging_state": "Disconnected",
            "charger_power": "0",
            "driveNumber": "0",
            "chargeNumber": "0",
        }
    )
    d.update(kw)
    return ",".join('"%s"' % d[c] for c in COLS)


def csv_text(rows):
    return ",".join(COLS) + "\n" + "\n".join(rows) + "\n"


def build():
    r = []
    # asleep, no vin/name reported (real exports blank these while sleeping)
    for i in range(5):
        r.append(row(f"2026-08-01 00:{i:02d}:00", display_name="", vin="", state="asleep", charging_state="", battery_level="80"))
    # drive 41: 10 min, 6 miles
    for i in range(10):
        r.append(
            row(
                f"2026-08-01 08:{i:02d}:00",
                shift_state="D",
                driveNumber="41",
                odometer=f"{1000 + i * 0.6:.2f}",
                battery_level=f"{80 - i * 0.3:.1f}",
                battery_range=f"{250 - i:.1f}",
                outside_tempF="70",
                latitude=f"{40.71 + i*0.001:.5f}",
                longitude="-74.00",
                heading="90",
                speed="35",
            )
        )
    # parked
    for i in range(3):
        r.append(row(f"2026-08-01 09:{i:02d}:00", battery_level="77"))
    # charge 12: 30 min at 11 kW, DC
    for i in range(30):
        r.append(
            row(
                f"2026-08-01 10:{i:02d}:00",
                charging_state="Charging",
                charger_power="11",
                chargeNumber="12",
                battery_level=f"{77 + i*0.4:.1f}",
                charge_energy_added=f"{i*0.18:.2f}",
                fast_charger_present="True",
                latitude="40.80",
                longitude="-74.01",
            )
        )
    # junk values a real export contains
    r.append(row("2026-08-01 11:00:00", shift_state="None", battery_level="89", outside_tempF="NULL", latitude="<invalid>", longitude=""))
    return csv_text(r)


def test_parse_handles_the_real_null_spellings():
    s = parse(build(), tz=PT)
    assert len(s) == 49
    last = s[-1]
    assert last["driving"] is False  # "None" is not a gear
    assert last["outside_f"] is None  # "NULL"
    assert last["latitude"] is None  # "<invalid>"
    assert s[0]["time"] == datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)  # local PDT -> UTC


def test_vehicle_identity_survives_blank_sleeping_rows():
    s = parse(build(), tz=PT)
    assert s[0]["vin"] is None  # sleeping rows carry no vin
    v = vehicle_of(s)
    assert v["vin"] == "5YJ3E1EA0JF000001" and v["name"] == "Bravo" and v["model"] == "models3"


def test_state_mapping():
    s = parse(build(), tz=PT)
    assert vehicle_state(s[0]) == "OFFLINE"
    assert vehicle_state(s[6]) == "DRIVING"
    assert vehicle_state(s[-1]) == "PARKED"


def test_replay_uses_teslafi_session_numbers():
    out = replay(parse(build(), tz=PT))
    assert len(out["trips"]) == 1
    t = out["trips"][0]
    assert round(t["destination_odometer"] - t["start_odometer"], 2) == 5.40
    assert t["start_position_latitude"] == 40.71
    assert len(out["charging_sessions"]) == 1
    c = out["charging_sessions"][0]
    assert c["start_level"] == 77.0 and c["charging_type"] == "DC"
    assert round(c["real_charged"], 2) == 5.22  # sum of increments from 0.00 to 5.22
    assert [v for _, _, v in out["states"]] == ["OFFLINE", "DRIVING", "PARKED"]
    assert [v for _, _, v in out["charging_powers"]] == [0.0, 11.0, 0.0]  # idle, the session, idle
    # 10 drive rows, then the charge sits still: one position, not thirty (matches the live recorder)
    assert len(out["positions"]) == 11
    assert out["positions"][0][6] == 35.0  # speed rides along
    assert out["positions"][0][7] == "DRIVING"
    assert out["vehicle"]["vin"] == "5YJ3E1EA0JF000001" and out["rows"] == 49
    assert [v for _, _, v in out["software"]] == ["2018.32.2"]


def test_software_history_is_a_run_per_version():
    r = [
        row("2026-08-01 08:00:00", car_version="2026.20.1"),
        row("2026-08-01 08:01:00", car_version=""),
        row("2026-08-01 08:02:00", car_version="2026.20.1"),
        row("2026-08-02 08:00:00", car_version="2026.26.3"),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [(a.day, v) for a, _, v in out["software"]] == [(1, "2026.20.1"), (2, "2026.26.3")]


def test_dst_fallback_hour_becomes_pst_not_a_pileup():
    # 2025-11-02: 01:00-01:59 local happens twice. Second pass is PST, one hour later in UTC.
    r = [
        row("2025-11-02 01:30:00", battery_level="50"),
        row("2025-11-02 01:59:00", battery_level="50"),
        row("2025-11-02 01:00:00", battery_level="49"),
        row("2025-11-02 01:30:00", battery_level="48"),
        row("2025-11-02 02:00:00", battery_level="47"),
    ]
    out = replay(iter_samples(io.StringIO(csv_text(r)), tz=PT))
    firsts = [a for a, _, _ in out["drive_levels"]]
    assert firsts == sorted(firsts) and len(set(firsts)) == len(firsts)
    assert [a.hour for a in firsts] == [8, 9, 9, 10]  # 01:30 PDT, 01:00 PST, 01:30 PST, 02:00 PST in UTC
    assert out["first"] <= out["last"]


def test_seconds_of_jitter_clamp_and_do_not_become_a_fold():
    r = [row("2021-08-29 09:31:32", battery_level="50"), row("2021-08-29 09:31:30", battery_level="51"), row("2021-08-29 09:32:30", battery_level="52")]
    out = replay(parse(csv_text(r), tz=PT)) if False else replay(iter_samples(io.StringIO(csv_text(r)), tz=PT))
    firsts = [a for a, _, _ in out["drive_levels"]]
    assert [a.strftime("%H:%M:%S") for a in firsts] == ["16:31:32", "16:32:30"]  # jittered row replaced the same-second run


def test_same_second_value_change_replaces_rather_than_twins():
    r = [row("2026-08-01 08:00:00", battery_level="50"), row("2026-08-01 08:00:00", battery_level="51"), row("2026-08-01 08:01:00", battery_level="51")]
    out = replay(parse(csv_text(r), tz=PT))
    assert [(v, a.minute, b.minute) for a, b, v in out["drive_levels"]] == [(51.0, 0, 1)]


def test_streams_from_gzip_without_materializing():
    blob = gzip.compress(build().encode())
    with gzip.open(io.BytesIO(blob), "rt", newline="") as fh:
        out = replay(iter_samples(fh, tz=PT))
    assert len(out["trips"]) == 1 and out["rows"] == 49


def test_empty_input():
    assert replay([])["trips"] == [] and parse("") == []


def test_model_comes_from_the_vin_when_car_type_is_useless():
    assert model_from_vin("5YJ3E1EA0JF000001") == "Model 3"
    assert model_from_vin("7SAYGDEE0NF000002") == "Model Y"
    r = [row("2026-08-01 08:00:00", car_type="model")]
    assert replay(parse(csv_text(r), tz=PT))["vehicle"]["model"] == "Model 3"


def test_energy_added_ignores_the_stale_value_from_the_previous_session():
    r = [row(f"2026-08-01 10:{i:02d}:00", charging_state="Charging", chargeNumber="5", charge_energy_added=v) for i, v in enumerate(["62.29", "62.29", "0", "0.4", "0.9", "1.5"])]
    out = replay(parse(csv_text(r), tz=PT))
    assert round(out["charging_sessions"][0]["real_charged"], 2) == 1.5
    # two charges under one number: both count
    r = [row(f"2026-08-01 11:{i:02d}:00", charging_state="Charging", chargeNumber="6", charge_energy_added=v) for i, v in enumerate(["0", "2", "4", "0", "1", "3"])]
    assert round(replay(parse(csv_text(r), tz=PT))["charging_sessions"][0]["real_charged"], 2) == 7.0


def test_a_stuck_session_number_splits_on_a_gap():
    r = [
        row("2026-08-01 08:00:00", shift_state="D", driveNumber="9", odometer="100"),
        row("2026-08-01 08:10:00", shift_state="D", driveNumber="9", odometer="105"),
        row("2026-08-03 08:00:00", shift_state="D", driveNumber="9", odometer="200"),
        row("2026-08-03 08:10:00", shift_state="D", driveNumber="9", odometer="207"),
        row("2026-08-01 20:00:00", charging_state="Charging", chargeNumber="3", charge_energy_added="0"),
        row("2026-08-01 20:30:00", charging_state="Charging", chargeNumber="3", charge_energy_added="4"),
        row("2026-08-05 20:00:00", charging_state="Charging", chargeNumber="3", charge_energy_added="0"),
        row("2026-08-05 20:30:00", charging_state="Charging", chargeNumber="3", charge_energy_added="6"),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [round(t["destination_odometer"] - t["start_odometer"]) for t in out["trips"]] == [5, 7]
    assert [c["real_charged"] for c in out["charging_sessions"]] == [4.0, 6.0]


def test_power_runs_end_when_charging_stops():
    r = [
        row("2026-08-01 10:00:00", charging_state="Charging", charger_power="11", chargeNumber="1"),
        row("2026-08-01 10:05:00", charging_state="Complete", charger_power="0"),
        row("2026-08-02 10:00:00", charging_state="Charging", charger_power="11", chargeNumber="2"),
        row("2026-08-02 10:05:00", charging_state="Charging", charger_power="11", chargeNumber="2"),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [(a.day, b.day, v) for a, b, v in out["charging_powers"]] == [(1, 1, 11.0), (1, 1, 0.0), (2, 2, 11.0)]


def test_fast_charger_flag_in_both_spellings():
    r = [
        row("2026-08-01 10:00:00", charging_state="Charging", chargeNumber="1", fast_charger_present="1", fast_charger_type="Tesla"),
        row("2026-08-01 11:00:00", charging_state="Charging", chargeNumber="2", fast_charger_present="0", fast_charger_type="MCSingleWireCAN"),
        row("2026-08-01 12:00:00", charging_state="Charging", chargeNumber="3", fast_charger_present="True", fast_charger_type=""),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [c["charging_type"] for c in out["charging_sessions"]] == ["DC", "AC", "DC"]
    assert [c.get("network") for c in out["charging_sessions"]] == ["supercharger", None, None], "only the Tesla type is a Supercharger"


def test_modes_inside_temperature_and_elevation_ride_along():
    r = [
        row("2026-08-01 08:00:00", sentry_mode="1", is_climate_on="0", inside_tempF="95", elevation="120.5", latitude="40.71", longitude="-74.00", shift_state="D", driveNumber="3"),
        row("2026-08-01 08:01:00", sentry_mode="1", is_climate_on="1", inside_tempF="90", elevation="131.0", latitude="40.80", longitude="-74.00", shift_state="D", driveNumber="3"),
        row("2026-08-01 08:02:00", sentry_mode="True", is_climate_on="True", inside_tempF="85", elevation="140.0", latitude="37.81", longitude="-74.00", shift_state="D", driveNumber="3"),
        row("2026-08-01 09:00:00", state="asleep", sentry_mode="", is_climate_on="", inside_tempF=""),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [v for _, _, v in out["modes"]] == [(True, False), (True, True)]  # asleep rows do not assert a mode
    assert [v for _, _, v in out["inside_temperatures"]] == [95.0, 90.0, 85.0]
    assert [p[8] for p in out["positions"]] == [120.5, 131.0, 140.0]


def test_mode_encoding_switches_eras_when_a_3_or_2_appears():
    r = [
        row("2024-08-01 08:00:00", sentry_mode="1", is_climate_on="1", fan_status="3"),  # old era: 1 is on
        row("2024-08-01 08:01:00", sentry_mode="0", is_climate_on="0", fan_status="0"),
        row("2025-08-01 08:00:00", sentry_mode="3", is_climate_on="2", fan_status="4"),  # new era begins
        row("2025-08-01 08:01:00", sentry_mode="1", is_climate_on="1", fan_status="0"),  # now 1 is idle
        row("2025-08-01 08:02:00", sentry_mode="1", is_climate_on="1", fan_status="2"),
    ]  # unless the fan is running
    out = replay(parse(csv_text(r), tz=PT))
    assert [v for _, _, v in out["modes"]] == [(True, True), (False, False), (True, True), (False, False), (False, True)]


def test_charging_electrics_run_while_charging_only():
    r = [
        row("2026-08-01 10:00:00", charging_state="Charging", chargeNumber="1", charger_voltage="240", charger_actual_current="48"),
        row("2026-08-01 10:01:00", charging_state="Charging", chargeNumber="1", charger_voltage="240", charger_actual_current="48"),
        row("2026-08-01 10:02:00", charging_state="Charging", chargeNumber="1", charger_voltage="236", charger_actual_current="30"),
        row("2026-08-01 10:03:00", charging_state="Complete", charger_voltage="236", charger_actual_current="0"),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [v for _, _, v in out["charging_electrics"]] == [(240.0, 48.0), (236.0, 30.0)]


def test_fsd_miles_per_drive_from_the_cars_own_meter():
    r = [
        row("2026-08-01 08:00:00", shift_state="D", driveNumber="7", odometer="100.0", SelfDrivingMilesSinceReset="3888.0"),
        row("2026-08-01 08:10:00", shift_state="D", driveNumber="7", odometer="106.0", SelfDrivingMilesSinceReset="3892.5"),
        row("2026-08-01 08:20:00", shift_state="D", driveNumber="7", odometer="110.0", SelfDrivingMilesSinceReset="3892.5"),
        row("2026-08-01 09:00:00", shift_state="D", driveNumber="8", odometer="110.0", SelfDrivingMilesSinceReset="0"),  # meter reset mid-history
        row("2026-08-01 09:10:00", shift_state="D", driveNumber="8", odometer="115.0", SelfDrivingMilesSinceReset="100"),
    ]  # rose more than the drive: not believed
    out = replay(parse(csv_text(r), tz=PT))
    assert [t["fsd_miles"] for t in out["trips"]] == [4.5, None]


def test_sleep_number_marks_sleep_when_state_says_online():
    r = [
        row("2026-08-01 02:00:00", state="offline", sleepNumber="9583", battery_level="48"),
        row("2026-08-01 03:00:00", state="", vin="", display_name="", battery_level="", sleepNumber="0"),  # an empty poll mid-sleep
        row("2026-08-01 05:00:00", state="online", sleepNumber="9583", battery_level="48"),
        row("2026-08-01 07:40:00", state="online", sleepNumber="0", idleNumber="17218", battery_level="48"),
    ]
    out = replay(parse(csv_text(r), tz=PT))
    assert [(a.hour, b.hour, v) for a, b, v in out["states"]] == [(9, 12, "OFFLINE"), (14, 14, "PARKED")]  # one sleep 02:00–05:00 PDT, not cut by the empty poll


def test_plugged_state_reads_the_cable_off_charging_state():
    from teslafi_import import plugged_state

    for v in ("Charging", "Complete", "Stopped", "Starting"):
        assert plugged_state(v) is True, v
    for v in ("Disconnected", "NoPower"):
        assert plugged_state(v) is False, v
    for v in (None, "", "None", "NULL", "<invalid>", "Something new"):
        assert plugged_state(v) is None, repr(v)


def test_charge_limit_reads_the_target_off_the_row():
    """The Tesla card's gauge marks the limit the car is set to charge to, as the live Lucid one does."""
    from teslafi_import import charge_limit

    assert charge_limit("80") == 80.0 and charge_limit("90.0") == 90.0
    for v in (None, "", "  ", "0", "-5", "101", "abc"):
        assert charge_limit(v) is None, v

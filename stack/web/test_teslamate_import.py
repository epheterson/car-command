from datetime import datetime, timezone
from pathlib import Path

from teslamate_import import replay_dir


def write(d: Path, name: str, header: str, rows: list[str]) -> None:
    (d / f"{name}.csv").write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))


def test_teslamate_dump_becomes_the_same_history(tmp_path):
    write(
        tmp_path,
        "states",
        "id,state,start_date,end_date,car_id",
        ["1,asleep,2026-08-20 05:00:00,2026-08-20 14:00:00,1", "2,online,2026-08-20 14:00:00,2026-08-20 18:00:00,1", "3,asleep,2026-08-20 18:00:00,2026-08-21 06:00:00,1"],
    )
    write(
        tmp_path,
        "positions",
        "id,date,latitude,longitude,speed,odometer,rated_battery_range_km,battery_level,outside_temp,drive_id",
        [f"{i},2026-08-20 15:{i:02d}:00,40.71{i},-74.00,{60 if 1 <= i <= 9 else ''},{1600 + i * 1.2:.1f},{400 - i * 2},{80 - i * 0.5},25,{7 if 1 <= i <= 9 else ''}" for i in range(0, 12)]
        + ["100,2026-08-20 16:00:00,40.80,-74.01,,1613.2,380,75,25,"],
    )
    write(
        tmp_path,
        "charges",
        "id,date,battery_level,charge_energy_added,charger_power,fast_charger_present,rated_battery_range_km,outside_temp,charging_process_id",
        [f"{i},2026-08-20 16:{i:02d}:00,{75 + i * 0.5},{i * 0.19:.2f},11,f,{380 + i},24,3" for i in range(0, 30)],
    )
    write(tmp_path, "charging_processes", "id,start_date,end_date,charge_energy_added,position_id,car_id", ["3,2026-08-20 16:00:00,2026-08-20 16:29:00,5.51,100,1"])
    write(tmp_path, "updates", "id,version,start_date,end_date,car_id", ["1,3.6.4,2026-08-17 22:25:50,,1"])
    out = replay_dir(tmp_path, "7UUG1GHJ0TA000003", "Alpha")
    assert out["vehicle"] == {"vin": "7UUG1GHJ0TA000003", "name": "Alpha"}
    (t,) = out["trips"]
    assert round(t["destination_odometer"] - t["start_odometer"], 2) == round(8 * 1.2 * 0.621371, 2)  # km became miles
    assert t["start_date"] == datetime(2026, 8, 20, 15, 1, tzinfo=timezone.utc)  # naive UTC, not local
    (c,) = out["charging_sessions"]
    assert c["charging_type"] == "AC" and round(c["real_charged"], 2) == 5.51 and c["session_position_latitude"] == 40.80
    assert [v for _, _, v in out["states"]][:3] == ["OFFLINE", "PARKED", "DRIVING"]
    assert out["states"][0][1] == datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)  # the sleep spans its state row
    assert out["outside_temperatures"][0][2] == 77.0  # 25 C
    assert [v for _, _, v in out["software"]] == ["3.6.4"]
    assert out["positions"][0][6] == 0.0 or out["positions"][1][6] > 30  # speed in mph

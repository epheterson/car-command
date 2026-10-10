"""Tesla bills each streamed signal, so a signal that changes by less than the site can use is not asked for.

Tesla's documentation (developer.tesla.com/docs/fleet-api/fleet-telemetry and .../billing-and-limits):
  - a field "is only sent to the event collector once two conditions are met: interval_seconds has elapsed since
    the field was last emitted and the field's value has changed", so the enum fields (Gear, ChargeState,
    DetailedChargeState, SentryMode) already send on change only;
  - "Location fields now support minimum_delta. Changes in distance are measured in meters." (firmware 2025.2.6)
    and, under cost optimisation, "utilize the minimum_delta configuration option for numeric fields";
  - the shape is "fields": {"VehicleSpeed": {"interval_seconds": 10}}, with minimum_delta beside interval_seconds.
resend_interval_seconds is not documented on those pages, so it is not used.

The drive below is synthetic (a seeded mix of stops, city, arterial and highway), because the repository holds no
recorded stream. It is judged the way the site uses the signals: the route and its length (Location), the top
speed (VehicleSpeed), peak draw and regeneration (PackVoltage x PackCurrent: the drive page shows peaks and regen
minutes, never an integral), and the energy a held value would give."""

import math
import random

import tesla_net


def drive(seconds=2400, seed=7):
    """One reading a second: position in metres, speed mph, heading, pack volts and amps (amps negative when drawing)."""
    rng = random.Random(seed)
    v, heading, x, y, target = 0.0, 90.0, 0.0, 0.0, 0.0
    rows = []
    for t in range(seconds):
        if t % 75 == 0:
            target = rng.choice([0.0, 0.0, 9.0, 13.0, 18.0, 25.0, 29.0])
        a = max(-4.0, min(2.5, (target - v) * 0.4 + rng.gauss(0, 0.12)))
        v = max(0.0, v + a)
        if v > 1 and rng.random() < 0.015:
            heading += rng.choice([-90, 90, -25, 25, -6, 6])
        if v > 1:
            heading += rng.gauss(0, 0.3)
        x += v * math.sin(math.radians(heading))
        y += v * math.cos(math.radians(heading))
        mech = 2000 * a * v + 0.5 * 1.2 * 0.23 * 2.2 * v**3 + 0.011 * 2000 * 9.81 * v
        watts = mech / 0.9 if mech > 0 else mech * 0.6
        watts += 400  # the car's own draw
        volts = 380 - 0.0004 * watts / 1.0
        amps = -watts / volts
        rows.append(
            {
                "x": x,
                "y": y,
                "mph": round(v * 2.23694, 1),
                "heading": round(heading % 360, 1),
                "volts": round(volts + rng.gauss(0, 0.08), 1),
                "amps": round(amps + rng.gauss(0, 0.4), 1),
            }
        )
    return rows


def sent(values, delta):
    """Tesla's rule at one reading a second: a value goes out when it differs from the last one sent (and, with a
    minimum_delta, by at least that). Returns what a listener holds each second, and how many were sent."""
    held, last, count = [], None, 0
    for v in values:
        if last is None or (abs(v - last) >= delta if delta else v != last):
            last, count = v, count + 1
        held.append(last)
    return held, count


def sent_position(rows, delta):
    last, held, count = None, [], 0
    for r in rows:
        if last is None or math.hypot(r["x"] - last[0], r["y"] - last[1]) >= max(delta, 1e-9):
            last, count = (r["x"], r["y"]), count + 1
        held.append(last)
    return held, count


def length(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def judge(rows, deltas):
    out = {}
    pos, n_loc = sent_position(rows, deltas.get("Location", 0))
    speed, n_speed = sent([r["mph"] for r in rows], deltas.get("VehicleSpeed", 0))
    head, n_head = sent([r["heading"] for r in rows], deltas.get("GpsHeading", 0))
    volts, n_volts = sent([r["volts"] for r in rows], deltas.get("PackVoltage", 0))
    amps, n_amps = sent([r["amps"] for r in rows], deltas.get("PackCurrent", 0))
    true_kw = [-r["volts"] * r["amps"] / 1000 for r in rows]
    held_kw = [-v * a / 1000 for v, a in zip(volts, amps)]
    out["signals"] = n_loc + n_speed + n_head + n_volts + n_amps
    out["length_error"] = abs(length(pos) - length([(r["x"], r["y"]) for r in rows])) / length([(r["x"], r["y"]) for r in rows])
    out["top_speed_error_mph"] = max(r["mph"] for r in rows) - max(speed)
    out["energy_error"] = abs(sum(held_kw) - sum(true_kw)) / sum(true_kw)
    out["peak_draw_error"] = (max(true_kw) - max(held_kw)) / max(true_kw)
    out["peak_regen_error"] = (abs(min(true_kw)) - abs(min(held_kw))) / abs(min(true_kw))
    out["regen_seconds_error"] = abs(sum(1 for k in held_kw if k < 0) - sum(1 for k in true_kw if k < 0)) / len(rows)
    out["heading_error_deg"] = max(abs(((a - b + 180) % 360) - 180) for a, b in zip(head, [r["heading"] for r in rows]))
    return out


def test_the_chosen_deltas_cost_the_drive_little_and_halve_its_signals():
    rows = drive()
    before = judge(rows, {})
    after = judge(rows, tesla_net.DELTAS)
    assert after["signals"] <= 0.55 * before["signals"], (before["signals"], after["signals"])
    assert after["length_error"] < 0.005, "the route is as long as before, to half a percent"
    assert 0 <= after["top_speed_error_mph"] <= tesla_net.DELTAS["VehicleSpeed"], "the top speed is never more than the step low"
    assert after["energy_error"] < 0.01, "the energy a held reading would give, within 1 %"
    assert 0 <= after["peak_draw_error"] < 0.02 and 0 <= after["peak_regen_error"] < 0.03, "peak draw and regeneration"
    assert after["regen_seconds_error"] < 0.005, "the regeneration minutes: off by under half a percent of the drive"
    assert after["heading_error_deg"] < tesla_net.DELTAS["GpsHeading"]


def test_a_parked_car_with_a_jittering_fix_sends_nothing_new():
    rng = random.Random(3)
    rows = [{"x": rng.gauss(0, 0.4), "y": rng.gauss(0, 0.4)} for _ in range(3600)]
    _, plain = sent_position(rows, 1e-9)
    _, with_delta = sent_position(rows, tesla_net.DELTAS["Location"])
    assert plain > 3000 and with_delta < 5, (plain, with_delta)


def charge(seconds=36000, seed=5):
    """An overnight AC charge: 32 A at 240 V, the pack voltage creeping up, a taper at the end."""
    rng = random.Random(seed)
    rows = []
    for t in range(seconds):
        amps = 32.0 if t < seconds * 0.93 else 32.0 * (1 - (t - seconds * 0.93) / (seconds * 0.07))
        rows.append({"volts": round(360 + 35 * t / seconds + rng.gauss(0, 0.08), 1), "amps": round(amps + rng.gauss(0, 0.25), 1)})
    return rows


def test_a_charge_sends_a_fraction_of_the_pack_readings_and_the_same_energy():
    rows = charge()
    plain = sum(sent([r[k] for r in rows], 0)[1] for k in ("volts", "amps"))
    volts, n_v = sent([r["volts"] for r in rows], tesla_net.DELTAS["PackVoltage"])
    amps, n_a = sent([r["amps"] for r in rows], tesla_net.DELTAS["PackCurrent"])
    assert n_v + n_a < 0.05 * plain, (plain, n_v + n_a)
    true_kwh = sum(r["volts"] * r["amps"] for r in rows) / 3.6e6
    held_kwh = sum(v * a for v, a in zip(volts, amps)) / 3.6e6
    assert abs(held_kwh - true_kwh) / true_kwh < 0.01, "an overnight charge's energy from held readings, within 1 %"


# ---- what is asked of the car


def test_only_documented_options_are_sent_in_the_documented_shape():
    cfg = tesla_net.config("stream.example.test", now=1_000_000)["fields"]
    assert cfg["Location"] == {"interval_seconds": 1, "minimum_delta": 3}
    assert cfg["VehicleSpeed"] == {"interval_seconds": 1, "minimum_delta": 0.5}
    assert cfg["GpsHeading"] == {"interval_seconds": 1, "minimum_delta": 5}
    assert cfg["PackVoltage"] == {"interval_seconds": 1, "minimum_delta": 1} and cfg["PackCurrent"] == {"interval_seconds": 1, "minimum_delta": 2}
    for field in ("Gear", "ChargeState", "DetailedChargeState", "SentryMode"):
        assert cfg[field] == {"interval_seconds": 1}, "already sent on change only"
    allowed = {"interval_seconds", "minimum_delta"}
    assert all(set(v) <= allowed for v in cfg.values()), "no resend_interval_seconds or anything undocumented"
    assert all(v["interval_seconds"] == tesla_net.FIELDS[k] for k, v in cfg.items())


def test_a_step_is_asked_only_of_a_number_that_moves_smoothly():
    assert set(tesla_net.DELTAS) <= set(tesla_net.FIELDS)
    for enum_like in ("Gear", "ChargeState", "DetailedChargeState", "SentryMode", "Locked", "DoorState", "Version", "VehicleName"):
        assert enum_like not in tesla_net.DELTAS


def test_the_route_stays_at_a_second_a_reading():
    assert tesla_net.FIELDS["Location"] == 1 and tesla_net.FIELDS["VehicleSpeed"] == 1
    assert tesla_net.DELTAS["Location"] <= 3, "a crawl in traffic still draws a line, as the recorder's own 1 m threshold would"


def test_the_new_steps_change_the_version_so_started_cars_are_sent_them():
    import hashlib

    v = tesla_net.fields_version()
    without = hashlib.sha256(repr(sorted(tesla_net.FIELDS.items())).encode()).hexdigest()[:12]
    assert v != without
    assert v != tesla_net.fields_version({**tesla_net.FIELDS, "MinutesToArrival": 60})


def test_a_config_tesla_holds_without_the_steps_is_outdated():
    import teslapage

    full = tesla_net.config("stream.example.test")["fields"]
    assert teslapage.outdated({"fields": full}, None) is False
    plain = {k: {"interval_seconds": v["interval_seconds"]} for k, v in full.items()}
    assert teslapage.outdated({"fields": plain}, None) is True, "same intervals, no steps: the car still streams at the old rate"
    assert teslapage.outdated({"fields": {**full, "PackCurrent": {"interval_seconds": 1, "minimum_delta": 5}}}, None) is True

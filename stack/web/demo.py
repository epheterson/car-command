"""A demo car: six weeks of a fictional Model 3 around Springfield, as a TeslaFi export.

For screenshots that show no real street, and for a stranger's first look at an empty
install: `make demo` generates the export and imports it, so every page fills in with a
car that never existed. The shape is a real commuter's — weekday runs to work, groceries
on the way home, a lake on Saturdays, one airport run, overnight AC charging at home on a
time-of-use plan and one fast charge on the road — and the rows are exactly the columns
the importer reads. Deterministic: the same seed makes the same six weeks.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import random
from bisect import bisect_right
from functools import cache
from pathlib import Path
from datetime import datetime, timedelta

VIN = "5YJ3E1EA0JF000001"
NAME = "Comet"
MODEL = "model3"
PAINT = "deepblue"
VERSION = "2026.20.6"
CAPACITY_KWH = 75.0
RATED_MI_AT_100 = 310.0
WH_PER_MI = 255.0
TICKS = 4  # rows a minute while driving

# Springfield, Illinois. The Capitol is Work; Home is a street a few miles east. Each point
# sits on a road, as brand/demo-routes.py snapped it.
PLACES = {
    "Home": (39.78167, -89.6008),
    "Work": (39.79889, -89.65338),
    "Grocery": (39.7605, -89.64219),
    "Lake": (39.707, -89.61302),
    "Airport": (39.84507, -89.67217),
}
# Every leg the generator drives, from and to; demo_routes.json (made by brand/demo-routes.py)
# holds the road each one follows, so drives run along streets and never across the lake.
LEGS = [
    ("Home", "Work"),
    ("Work", "Home"),
    ("Work", "Grocery"),
    ("Grocery", "Home"),
    ("Home", "Grocery"),
    ("Home", "Lake"),
    ("Lake", "Home"),
    ("Home", "Airport"),
    ("Airport", "Home"),
]
# The demo's home plan, so its charges have a cost on an install that has none: off-peak
# $0.40, part-peak $0.60 3-4pm all year and 9-10pm outside June-September, peak $0.63 4-9pm.
PLAN_NAME = "Springfield EV"
RATES = [{"from": "2000-01-01", "off_peak": 0.40, "part_peak": 0.60, "peak": 0.63}]
SCHEDULE = [
    {"period": "peak", "days": "all", "start": 16, "end": 21, "months": None},
    {"period": "part_peak", "days": "all", "start": 15, "end": 16, "months": None},
    {"period": "part_peak", "days": "all", "start": 21, "end": 22, "months": [1, 2, 3, 4, 5, 10, 11, 12]},
]
COLUMNS = [
    "Date",
    "vin",
    "display_name",
    "car_type",
    "car_version",
    "state",
    "shift_state",
    "battery_level",
    "battery_range",
    "odometer",
    "latitude",
    "longitude",
    "heading",
    "speed",
    "power",
    "elevation",
    "outside_tempF",
    "inside_tempF",
    "charging_state",
    "charger_power",
    "charger_voltage",
    "charger_actual_current",
    "charge_energy_added",
    "fast_charger_present",
    "fast_charger_type",
    "driveNumber",
    "chargeNumber",
    "sleepNumber",
    "sentry_mode",
    "is_climate_on",
    "fan_status",
    "SelfDrivingMilesSinceReset",
]


def distance_mi(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


class Route:
    """A road as a polyline of (lat, lon), walked by distance."""

    def __init__(self, points):
        self.points = [tuple(p) for p in points]
        self.cum = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.cum.append(self.cum[-1] + distance_mi(a, b))
        self.miles = self.cum[-1]

    def at(self, f: float):
        """(position, heading in degrees) a fraction `f` of the way along."""
        d = max(0.0, min(1.0, f)) * self.miles
        i = min(max(bisect_right(self.cum, d) - 1, 0), len(self.points) - 2)
        a, b = self.points[i], self.points[i + 1]
        span = self.cum[i + 1] - self.cum[i]
        t = (d - self.cum[i]) / span if span else 0.0
        pos = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        heading = math.degrees(math.atan2((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])) % 360
        return pos, heading


@cache
def route(frm: str, to: str) -> Route:
    data = json.loads((Path(__file__).with_name("demo_routes.json")).read_text())
    return Route(data["routes"][f"{frm}>{to}"])


class Car:
    def __init__(self, rng: random.Random, start: datetime):
        self.rng = rng
        self.t = start
        self.level = 78.0
        self.odometer = 12_480.0
        self.at = PLACES["Home"]
        self.place = "Home"
        self.drive_no = 0
        self.charge_no = 0
        self.sleep_no = 0
        self.rows: list[dict] = []
        self.fsd = 1_204.0

    def row(self, **kw) -> None:
        temp = 62 + 14 * math.sin((self.t.timetuple().tm_yday + self.t.hour / 24 - 100) / 365 * 2 * math.pi) + 8 * math.sin((self.t.hour - 6) / 24 * 2 * math.pi)
        base = {
            "Date": self.t.strftime("%Y-%m-%d %H:%M:%S"),
            "vin": VIN,
            "display_name": NAME,
            "car_type": MODEL,
            "car_version": VERSION,
            "state": "online",
            "shift_state": "",
            "battery_level": f"{self.level:.2f}",
            "battery_range": f"{self.level / 100 * RATED_MI_AT_100:.2f}",
            "odometer": f"{self.odometer:.3f}",
            "latitude": f"{self.at[0]:.6f}",
            "longitude": f"{self.at[1]:.6f}",
            "heading": "0",
            "speed": "",
            "power": "0",
            "elevation": "180",
            "outside_tempF": f"{temp:.0f}",
            "inside_tempF": f"{temp + 4:.0f}",
            "charging_state": "Disconnected",
            "charger_power": "0",
            "charger_voltage": "0",
            "charger_actual_current": "0",
            "charge_energy_added": "0",
            "fast_charger_present": "False",
            "fast_charger_type": "<invalid>",
            "driveNumber": "",
            "chargeNumber": "",
            "sleepNumber": "",
            "sentry_mode": "False",
            "is_climate_on": "False",
            "fan_status": "0",
            "SelfDrivingMilesSinceReset": f"{self.fsd:.1f}",
        }
        base.update(kw)
        self.rows.append(base)

    def sleep_until(self, until: datetime) -> None:
        """Idle a few minutes, then asleep rows every 15 minutes until `until`."""
        idle_end = min(self.t + timedelta(minutes=8), until)
        while self.t < idle_end:
            self.row()
            self.t += timedelta(minutes=2)
        self.sleep_no += 1
        while self.t < until:
            self.level -= 0.004  # a night of phantom drain adds up to about a percent
            self.row(state="asleep", sleepNumber=str(self.sleep_no))
            self.t += timedelta(minutes=15)
        self.t = until

    def drive(self, to: str, fsd: bool = False) -> None:
        road = route(self.place, to)
        miles = road.miles
        self.drive_no += 1
        avg = self.rng.uniform(24, 34)
        minutes = max(4, int(miles / avg * 60))
        # Four rows a minute, so the line bends where the road does and not in chords between
        # corners; the pace and the drain are still set a minute at a time.
        steps = minutes * TICKS
        speed = 0.0
        for k in range(steps + 1):
            m = k // TICKS
            self.at, heading = road.at(k / steps)
            if k % TICKS == 0:
                speed = 0 if k in (0, steps) else max(8, min(68, avg * (1.2 + 0.6 * math.sin(m / minutes * math.pi * 2.5)) + self.rng.uniform(-6, 6)))
            step = miles / steps if k else 0
            self.odometer += step
            self.level -= step * WH_PER_MI / (CAPACITY_KWH * 1000) * 100
            if fsd:
                self.fsd += step
            self.row(shift_state="D", speed=f"{speed:.0f}", power=f"{(speed * 0.55 - (18 if m % 7 == 3 else 0)):.0f}", heading=f"{heading:.0f}", driveNumber=str(self.drive_no), inside_tempF=str(70))
            self.t += timedelta(seconds=60 // TICKS)
        self.at, self.place = PLACES[to], to
        self.row(shift_state="P", speed="0")
        self.t += timedelta(minutes=1)

    def charge(self, to_level: float, kw: float, fast: bool = False) -> None:
        self.charge_no += 1
        added = 0.0
        while self.level < to_level:
            step_min = 5
            energy = kw * step_min / 60
            self.level = min(to_level, self.level + energy / CAPACITY_KWH * 100)
            added += energy
            self.row(
                charging_state="Charging",
                charger_power=f"{kw:.0f}",
                charger_voltage="240" if not fast else "400",
                charger_actual_current=f"{kw * 1000 / 240:.0f}" if not fast else f"{kw * 1000 / 400:.0f}",
                charge_energy_added=f"{added:.2f}",
                chargeNumber=str(self.charge_no),
                fast_charger_present="True" if fast else "False",
                fast_charger_type="Tesla" if fast else "<invalid>",
            )
            self.t += timedelta(minutes=step_min)
        self.row(charging_state="Complete", charge_energy_added=f"{added:.2f}", chargeNumber=str(self.charge_no))
        self.t += timedelta(minutes=1)


def generate(days: int = 42, seed: int = 7, end: datetime | None = None) -> list[dict]:
    rng = random.Random(seed)
    end = end or datetime(2026, 9, 17, 6, 0)
    car = Car(rng, end - timedelta(days=days))
    car.t = car.t.replace(hour=6, minute=30)
    saturdays = 0
    # The newest full Saturday has the road charge, and the night before it a home charge, so a
    # day of the timeline shows both kinds, whatever day the demo is made on.
    last_sat = max(end.date() - timedelta(days=n) for n in range(1, 8) if (end.date() - timedelta(days=n)).weekday() == 5)
    for day in range(days):
        d = car.t.date()
        weekday = d.weekday()
        if weekday < 5:
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=7, minutes=rng.randint(35, 55)))
            car.drive("Work", fsd=rng.random() < 0.6)
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=17, minutes=rng.randint(5, 50)))
            if rng.random() < 0.35:
                car.drive("Grocery")
                car.sleep_until(car.t + timedelta(minutes=rng.randint(20, 40)))
            car.drive("Home")
        elif weekday == 5:
            saturdays += 1
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=rng.randint(0, 50)))
            if saturdays == 3:
                car.drive("Airport", fsd=True)
                car.sleep_until(car.t + timedelta(minutes=30))
                car.drive("Home", fsd=True)
            else:
                car.drive("Lake", fsd=rng.random() < 0.5)
                car.sleep_until(car.t + timedelta(hours=rng.randint(2, 4)))
                if d == last_sat:
                    car.charge(min(90.0, car.level + 14), 100.0, fast=True)
                car.drive("Home")
        else:
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=11))
            if rng.random() < 0.5:
                car.drive("Grocery")
                car.sleep_until(car.t + timedelta(minutes=35))
                car.drive("Home")
        # home overnight: plug in when under 65 %, charging starts at midnight (off-peak)
        car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(days=1, minutes=5))
        if car.level < 65 or (d == last_sat - timedelta(days=1) and car.level < 79):
            car.charge(80.0, 11.0)
        car.sleep_until(datetime.combine(d + timedelta(days=1), datetime.min.time()) + timedelta(hours=6, minutes=30))
    return car.rows


def export_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def write(path: str, days: int = 42, seed: int = 7) -> int:
    rows = generate(days, seed)
    data = export_csv(rows).encode("utf-8")
    if path.endswith(".gz"):
        with gzip.open(path, "wb") as fh:
            fh.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)
    return len(rows)


def mid_drive(now: datetime, level: float, odometer: float, frm: str = "Home", to: str = "Work", minutes: int = 11, minutes_in: int = 7, seed: int = 7) -> tuple[list[dict], list[dict]]:
    """Comet partway through a drive, for the banner at the top of Now: the TeslaFi feed's rows
    from the start until `now` (local, naive, like the export's Date), and the destination and
    time to go as the car's own stream sends them. Same pace and drain as Car.drive."""
    car = Car(random.Random(seed), now - timedelta(minutes=minutes_in))
    car.level, car.odometer, car.at = level, odometer, PLACES[frm]
    road = route(frm, to)
    miles = road.miles
    for m in range(minutes_in + 1):
        f = m / minutes
        step = miles / minutes if m else 0
        car.at, heading = road.at(f)
        car.odometer += step
        car.level -= step * WH_PER_MI / (CAPACITY_KWH * 1000) * 100
        speed = 0 if m == 0 else 20 + 6 * math.sin(m)
        car.row(shift_state="D", speed=f"{speed:.0f}", power=f"{speed * 0.55:.0f}", heading=f"{heading:.0f}", driveNumber="1", carState="Driving")
        car.t += timedelta(minutes=1)

    def stream(stamp: str, data: list[dict]) -> dict:
        return {"at": stamp, "record": {"vin": VIN, "data": data}}

    left = minutes - minutes_in
    nav = [
        stream(car.rows[0]["Date"], [{"key": "DestinationName", "value": {"stringValue": to}}]),
        stream(
            now.strftime("%Y-%m-%d %H:%M:%S"),
            [{"key": "MinutesToArrival", "value": {"doubleValue": float(left)}}, {"key": "MilesToArrival", "value": {"doubleValue": round(miles * left / minutes, 2)}}],
        ),
    ]
    return car.rows, nav


def put_mid_drive(conn, tz, now: datetime | None = None) -> int:
    """Write mid_drive() into a throwaway demo site: the feed rows (cc_teslafi_rows) and the
    stream's records (cc_tesla_raw), going on from where the imported history left the car.
    Only for the demo media (brand/make-demo-media.sh); a real install's rows come from the car."""
    import json

    from teslafi_live import DDL

    now = now or datetime.now(tz).replace(tzinfo=None, microsecond=0)
    last = conn.execute("SELECT level, odometer FROM cc_positions WHERE vin = %s ORDER BY at DESC LIMIT 1", (VIN,)).fetchone()
    level, odo = (last[0], last[1]) if last and last[0] is not None and last[1] is not None else (78.0, 12_480.0)
    rows, nav = mid_drive(now, float(level), float(odo))
    conn.execute(DDL)
    local = lambda stamp: datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)  # noqa: E731
    for r in rows:
        conn.execute("INSERT INTO cc_teslafi_rows (vin, at, data) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (VIN, local(r["Date"]), json.dumps(r)))
    for n in nav:
        conn.execute("INSERT INTO cc_tesla_raw (vin, at, kind, record) VALUES (%s, %s, 'V', %s) ON CONFLICT DO NOTHING", (VIN, local(n["at"]), json.dumps(n["record"])))
    conn.commit()
    return len(rows)


def demo_plan() -> dict:
    import cost

    return cost.compile_rows(PLAN_NAME, RATES, SCHEDULE)


def save_plan(conn) -> None:
    """The demo's plan as the install's, unless it already has one of its own."""
    import settings

    if settings.load(conn).get("rate_plan") is None:
        settings.save(conn, "rate_plan", demo_plan())


def prepare(app) -> None:
    """Before the import: name the places, so the charges at Home are home charges, and
    give the install the demo's plan, so they have a price, and its car a paint."""
    import identity

    for n, (la, lo) in PLACES.items():
        app.add_place(n, la, lo, 150)
    with app.db() as conn:
        save_plan(conn)
        identity.save_choice(conn, VIN, PAINT, None, None)  # the car's paint, so its colour is picked the usual way
        conn.commit()
    print("named", ", ".join(PLACES), "set the plan", PLAN_NAME, "and the paint", PAINT)


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "TeslaFiData-demo.csv.gz"
    print(f"{write(out)} rows -> {out}")

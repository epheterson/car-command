"""A demo car: six weeks of a fictional Model 3 around Springfield, as a TeslaFi export.

For screenshots that show no real street, and for a stranger's first look at an empty
install: `make demo` generates the export and imports it, so every page fills in with a
car that never existed. The shape is a real commuter's — weekday runs to work, groceries
on the way home, a lake on Saturdays, one airport run, overnight AC charging at home on a
time-of-use plan and one fast charge on the road — and the rows are exactly the columns
the importer reads. Deterministic: the same seed makes the same six weeks.

`years` puts that many years of lighter history in front of the six weeks (a row a
minute driving, an hour asleep), for the pages that need a long record: the pack losing
range with the miles, a software update every month or so, winters on their own tires,
and now and then a long Sunday loop.
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
MILES_A_YEAR = 4_400  # about what the commute and the weekends add up to
NOISE_SEED = 7919  # the pack estimate's wobble has a stream of its own

# Springfield, Illinois. The Capitol is Work; Home is a side street in a subdivision a few miles
# east, past the interstate. Each point sits on a road, as brand/demo-routes.py snapped it.
PLACES = {
    "Home": (39.765, -89.57488),
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
# The long Sunday drives of the older history: through these, starting and ending at Home.
LOOPS = (("Airport", "Home", "Lake"), ("Lake", "Home", "Airport"), ("Work", "Grocery", "Home", "Lake"), ("Lake", "Home", "Grocery"))
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


def outside_f(t: datetime) -> float:
    """A Springfield year: about 30 F on a January night, 85 on a July afternoon."""
    return 54 + 24 * math.sin((t.timetuple().tm_yday + t.hour / 24 - 100) / 365 * 2 * math.pi) + 8 * math.sin((t.hour - 6) / 24 * 2 * math.pi)


def full_range(odometer: float) -> float:
    """Rated miles at 100 % for a pack this many miles old: the quick first-years fade,
    then a slow line."""
    return RATED_MI_AT_100 * (1 - 0.045 * (1 - math.exp(-odometer / 12_000)) - 0.0035 * odometer / 10_000)


def drain_factor(temp_f: float, mph: float, winter_tires: bool = False) -> float:
    """How much more than WH_PER_MI a drive costs: cold, heat, pace, and softer tires."""
    k = 1 + 0.009 * max(0.0, 62 - temp_f) + 0.003 * max(0.0, temp_f - 78) + 0.007 * (mph - 30)
    return k * (1.05 if winter_tires else 1.0)


def version_for(d, prev: str | None = None) -> str:
    """A believable version name for an update that lands on day `d`: the build is a couple
    of months older than the day it arrives, and always newer than `prev`, the one it
    replaces (two updates in one fortnight's build take the next point release)."""
    year, week, _ = (d - timedelta(days=70)).isocalendar()
    name = f"{year}.{max(2, week // 2 * 2)}.{d.day % 9 + 1}"
    if prev and _vkey(name) <= _vkey(prev):
        y, w, n = _vkey(prev)
        name = f"{y}.{w}.{n + 1}"
    return name


def _vkey(name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in name.split("."))


def winters(start, end) -> list[tuple]:
    """(fitted, removed) for each winter between two dates: on in early December, off in
    late March, the last one still on if it has not come off yet."""
    out = []
    for y in range(start.year - 1, end.year + 1):
        on, off = datetime(y, 12, 1).date(), datetime(y + 1, 3, 25).date()
        if off > start and on < end:
            out.append((max(on, start), off if off < end else None))
    return out


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
def ground(at: tuple[float, float]) -> float:
    """Height above sea level in metres at a point: rolling hills from the position alone,
    so a place is the same height every visit and a road climbs and falls along its length."""
    lat, lon = at
    return 180 + 25 * math.sin(lat * 40) + 12 * math.cos(lon * 55) + 4 * math.sin((lat + lon) * 150)


def route(frm: str, to: str) -> Route:
    data = json.loads((Path(__file__).with_name("demo_routes.json")).read_text())
    return Route(data["routes"][f"{frm}>{to}"])


@cache
def loop(*stops: str) -> Route:
    """One drive through several places in a row, on the same roads as the single legs."""
    pts = []
    for a, b in zip(stops, stops[1:]):
        pts += route(a, b).points[1 if pts else 0 :]
    return Route(pts)


class Car:
    def __init__(self, rng: random.Random, start: datetime, odometer: float = 12_480.0):
        self.rng = rng
        self.t = start
        self.level = 78.0
        self.odometer = odometer
        self.at = PLACES["Home"]
        self.place = "Home"
        self.drive_no = 0
        self.charge_no = 0
        self.sleep_no = 0
        self.rows: list[dict] = []
        self.fsd = 1_204.0
        self.version = VERSION
        self.light = False  # older history: fewer rows, same story
        self.winter = False
        # The pack's own estimate wanders a little from charge to charge, and an update now
        # and then recalibrates it; a separate stream, so the drives stay where they were.
        self.noise = random.Random(NOISE_SEED)
        self.wobble = 0.0
        self.calibration = 0.0

    def full(self) -> float:
        """The pack's own estimate of its range at 100 %."""
        return full_range(self.odometer) + self.calibration + self.wobble

    def pack_kwh(self) -> float:
        """What the pack holds now: it fades with the range, so a rated mile stays the same
        energy for the life of the car."""
        return CAPACITY_KWH * full_range(self.odometer) / RATED_MI_AT_100

    def row(self, **kw) -> None:
        temp = outside_f(self.t)
        base = {
            "Date": self.t.strftime("%Y-%m-%d %H:%M:%S"),
            "vin": VIN,
            "display_name": NAME,
            "car_type": MODEL,
            "car_version": self.version,
            "state": "online",
            "shift_state": "",
            "battery_level": f"{self.level:.2f}",
            "battery_range": f"{self.level / 100 * self.full():.2f}",
            "odometer": f"{self.odometer:.3f}",
            "latitude": f"{self.at[0]:.6f}",
            "longitude": f"{self.at[1]:.6f}",
            "heading": "0",
            "speed": "",
            "power": "0",
            "elevation": f"{ground(self.at):.0f}",
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

    def guard(self, until: datetime) -> None:
        """Parked with Sentry on until `until`: awake the whole time, a row every 5 minutes,
        draining faster than asleep."""
        while self.t < until:
            self.level -= 0.03
            self.row(sentry_mode="True")
            self.t += timedelta(minutes=5)
        self.t = until

    def precondition(self, minutes: int = 8) -> None:
        """The cabin warmed or cooled to 70 °F before leaving: climate on, the fan running."""
        for k in range(0, minutes, 2):
            self.level -= 0.05
            outside = float(self.rows[-1]["outside_tempF"]) if self.rows else 62.0
            self.row(is_climate_on="True", fan_status="3", inside_tempF=f"{outside + 4 + (70 - outside - 4) * (k + 2) / minutes:.0f}")
            self.t += timedelta(minutes=2)

    def sleep_until(self, until: datetime) -> None:
        """Idle a few minutes, then asleep rows every 15 minutes until `until`."""
        idle_end = min(self.t + timedelta(minutes=8), until)
        while self.t < idle_end:
            self.row()
            self.t += timedelta(minutes=2)
        self.sleep_no += 1
        step = 60 if self.light else 15
        while self.t < until:
            self.level -= 0.004 * step / 15  # a night of phantom drain adds up to about a percent
            self.row(state="asleep", sleepNumber=str(self.sleep_no))
            self.t += timedelta(minutes=step)
        self.t = until

    def drive(self, to: str, fsd: bool = False, via: tuple = (), pace: tuple = (24, 34), top: float = 68) -> None:
        road = loop(self.place, *via, to) if via else route(self.place, to)
        miles = road.miles
        self.drive_no += 1
        avg = self.rng.uniform(*pace)
        minutes = max(4, int(miles / avg * 60))
        # Four rows a minute, so the line bends where the road does and not in chords between
        # corners; the pace and the drain are still set a minute at a time.
        ticks = 1 if self.light else TICKS
        steps = minutes * ticks
        wh = WH_PER_MI * drain_factor(outside_f(self.t), avg, self.winter)
        noise = was = 0.0
        for k in range(steps + 1):
            m = k // ticks
            self.at, heading = road.at(k / steps)
            if k % ticks == 0:
                was, noise = noise, self.rng.uniform(-6, 6)  # one draw a minute, as before, so the rest of the demo is unchanged
            # the pace every row, smooth through the minute, with a little wobble: a car's speed, not a staircase
            f = k / ticks
            speed = 0 if k in (0, steps) else max(8, min(top, avg * (1.2 + 0.6 * math.sin(f / minutes * math.pi * 2.5)) + was + (noise - was) * (k % ticks) / ticks + 2 * math.sin(k * 1.3)))
            step = miles / steps if k else 0
            self.odometer += step
            self.level -= step * wh / (self.pack_kwh() * 1000) * 100
            if fsd:
                self.fsd += step
            self.row(shift_state="D", speed=f"{speed:.0f}", power=f"{(speed * 0.55 - (18 if m % 7 == 3 else 0)):.0f}", heading=f"{heading:.0f}", driveNumber=str(self.drive_no), inside_tempF=str(70))
            self.t += timedelta(seconds=60 // ticks)
        self.at, self.place = PLACES[to], to
        self.row(shift_state="P", speed="0")
        self.t += timedelta(minutes=1)

    def charge(self, to_level: float, kw: float, fast: bool = False) -> None:
        self.charge_no += 1
        added = 0.0
        while self.level < to_level:
            # a fast charge a minute at a time, tapering as the pack fills; at home every five
            step_min = 1 if fast else 5
            kw_now = kw * max(0.3, min(1.0, (100 - self.level) / 50)) if fast else kw
            energy = kw_now * step_min / 60
            self.level = min(to_level, self.level + energy / self.pack_kwh() * 100)
            added += energy
            self.row(
                charging_state="Charging",
                charger_power=f"{kw_now:.0f}",
                charger_voltage="240" if not fast else "400",
                charger_actual_current=f"{kw_now * 1000 / 240:.0f}" if not fast else f"{kw_now * 1000 / 400:.0f}",
                charge_energy_added=f"{added:.2f}",
                chargeNumber=str(self.charge_no),
                fast_charger_present="True" if fast else "False",
                fast_charger_type="Tesla" if fast else "<invalid>",
            )
            self.t += timedelta(minutes=step_min)
        self.row(charging_state="Complete", charge_energy_added=f"{added:.2f}", chargeNumber=str(self.charge_no))
        self.t += timedelta(minutes=1)
        self.wobble = 0.6 * self.wobble + self.noise.gauss(0, 1.6)


def default_end(now: datetime | None = None) -> datetime:
    """Where the generated days stop: 06:00 today. The last day closes at 06:30, so `generate`
    cuts what it makes at the last completed hour when this is the end. Local, naive."""
    now = now or datetime.now()
    return now.replace(hour=6, minute=0, second=0, microsecond=0)


def generate(days: int = 42, seed: int = 7, end: datetime | None = None, years: int = 0) -> list[dict]:
    six = random.Random(seed)
    now = datetime.now()
    cut = None if end else now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    end = end or default_end(now)
    first = end - timedelta(days=days)
    total = days + 365 * years
    car = Car(six, end - timedelta(days=total), odometer=max(12.0, 12_480.0 - MILES_A_YEAR * years))
    car.t = car.t.replace(hour=6, minute=30)
    # The older years draw from their own stream and the six weeks from theirs, so the six
    # weeks' drives, times and paces are the same whatever comes before; what carries over
    # is the car's state (level, odometer, version, the pack's fade).
    old = random.Random(seed * 7919 + years)
    if years:
        car.version = version_for(car.t.date())
    next_update = car.t.date() + timedelta(days=old.randint(18, 45))
    tires = winters(car.t.date(), end.date()) if years else []
    saturdays, current = 0, None
    # The newest full Saturday has the road charge, and the night before it a home charge, so a
    # day of the timeline shows both kinds, whatever day the demo is made on.
    last_sat = max(end.date() - timedelta(days=n) for n in range(1, 8) if (end.date() - timedelta(days=n)).weekday() == 5)
    for day in range(total):
        d = car.t.date()
        weekday = d.weekday()
        car.light = car.t < first
        rng = car.rng = old if car.light else six
        car.winter = any(on <= d and (off is None or d < off) for on, off in tires)
        if car.light and d >= next_update:
            car.version = version_for(d, car.version)
            # now and then an update changes how the pack's range is estimated
            if old.random() < 0.3:
                car.calibration = max(-6.0, min(6.0, car.calibration + old.uniform(-3.0, 3.0)))
            next_update = d + timedelta(days=old.randint(18, 45))
        elif not car.light and car.version != current:
            # the six weeks run on one version, newer than the last of the older history's
            current = car.version = version_for(d, car.version) if years else VERSION
        if weekday < 5:
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=7, minutes=rng.randint(35, 55)))
            car.precondition()
            car.drive("Work", fsd=rng.random() < 0.6)
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=17, minutes=rng.randint(5, 50)))
            if rng.random() < 0.35:
                car.drive("Grocery")
                car.sleep_until(car.t + timedelta(minutes=rng.randint(20, 40)))
            car.drive("Home")
        elif weekday == 5:
            saturdays += 0 if car.light else 1
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=rng.randint(0, 50)))
            if saturdays == 3:
                car.drive("Airport", fsd=True)
                car.sleep_until(car.t + timedelta(minutes=30))
                car.drive("Home", fsd=True)
            else:
                car.drive("Lake", fsd=rng.random() < 0.5)
                car.guard(car.t + timedelta(hours=rng.randint(2, 4)))  # a car park by the water: Sentry on
                if d == last_sat:
                    car.charge(min(90.0, car.level + 14), 100.0, fast=True)
                elif car.light and old.random() < 0.12:
                    car.charge(min(90.0, car.level + old.uniform(10, 25)), old.choice((48.0, 72.0, 120.0, 150.0)), fast=True)
                car.drive("Home")
        else:
            car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(hours=11))
            if car.light and old.random() < 0.12:
                # a long Sunday loop through places it knows, home at the end
                car.drive("Home", fsd=old.random() < 0.5, via=old.choice(LOOPS), pace=(30, 46), top=old.choice((70, 74, 78)))
            elif rng.random() < 0.5:
                car.drive("Grocery")
                car.sleep_until(car.t + timedelta(minutes=35))
                car.drive("Home")
        # home overnight: plug in when under 65 %, charging starts at midnight (off-peak)
        car.sleep_until(datetime.combine(d, datetime.min.time()) + timedelta(days=1, minutes=5))
        if car.level < 65 or (d == last_sat - timedelta(days=1) and car.level < 79):
            car.charge(80.0, 11.0)
        car.sleep_until(datetime.combine(d + timedelta(days=1), datetime.min.time()) + timedelta(hours=6, minutes=30))
    # Loaded before 07:00, the last generated morning is still ahead: the history ends at the last
    # completed hour, so Now's last 24 hours and the Battery strip are full overnight too.
    return [r for r in car.rows if r["Date"] <= cut] if cut else car.rows


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
        car.level -= step * WH_PER_MI / (car.pack_kwh() * 1000) * 100
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


def tire_sets(end: datetime, years: int, days: int = 42) -> list[tuple]:
    """(name, fitted, removed) for the tires the long history drove on: winters on their
    own set, all-seasons the rest of the year. None without `years`."""
    if not years:
        return []
    start = (end - timedelta(days=days + 365 * years)).date()
    out, last = [], start
    for on, off in winters(start, end.date()):
        if on > last:
            out.append(("All-season", last, on))
        out.append(("Winter", on, off))
        last = off
    if last is not None and last < end.date():
        out.append(("All-season", last, None))
    return out


def prepare(app, end: datetime | None = None, years: int = 0) -> None:
    """Before the import: name the places, so the charges at Home are home charges, and
    give the install the demo's plan, so they have a price, and its car a paint. With
    `years` (the same `end` and `years` the export was made with), the tire sets too."""
    import identity

    for n, (la, lo) in PLACES.items():
        app.add_place(n, la, lo, 150)
    with app.db() as conn:
        save_plan(conn)
        identity.save_choice(conn, VIN, PAINT, None, None)  # the car's paint, so its colour is picked the usual way
        for name, on, off in tire_sets(end or default_end(), years):
            conn.execute("INSERT INTO cc_tire_sets (vin, name, fitted_on, removed_on) VALUES (%s, %s, %s, %s)", (VIN, name, on, off))
        conn.commit()
    print("named", ", ".join(PLACES), "set the plan", PLAN_NAME, "and the paint", PAINT)


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "TeslaFiData-demo.csv.gz"
    print(f"{write(out)} rows -> {out}")

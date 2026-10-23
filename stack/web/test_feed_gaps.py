"""The feed goes blind (Unknown, no odometer) and the stream saw the car: its samples fill the span."""

from datetime import datetime, timedelta, timezone

import feed_gaps
from teslafi_import import replay, sample_from_row

ALPHA = "5YJ3E1EA0JF000001"
T0 = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)
SPRINGFIELD = (39.78, -89.65)


def feed_row(minute, **kw):
    at = T0 + timedelta(minutes=minute)
    row = {"Date": "", "vin": ALPHA, "state": "online", "carState": "Idling", "odometer": "1000.0", "battery_level": "80", "latitude": "39.78", "longitude": "-89.65", "sleepNumber": "0"}
    row.update(kw)
    return sample_from_row(row, when=at)


def blind(minute):
    return sample_from_row({"Date": "", "state": "online", "carState": "Unknown", "sleepNumber": "0", "Notes": "No Telemetry Record"}, when=T0 + timedelta(minutes=minute))


def sleeping(minute):
    return sample_from_row({"Date": "", "vin": ALPHA, "state": "offline", "carState": "Sleeping", "sleepNumber": "10"}, when=T0 + timedelta(minutes=minute))


def pl(minute, **fields):
    data = []
    for key, v in fields.items():
        if isinstance(v, tuple):
            data.append({"key": key, "value": {"locationValue": {"latitude": v[0], "longitude": v[1]}}})
        elif key == "Gear":
            data.append({"key": key, "value": {"shiftStateValue": "ShiftState" + v}})
        else:
            data.append({"key": key, "value": {"doubleValue": v}})
    return {"data": data, "createdAt": (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"), "vin": ALPHA}


def stream_drive(start_min=36, miles=1.6):
    """Parked at odometer 1000, one drive of five minutes, parked again."""
    out = [pl(30, Odometer=1000.0, Gear="P", VehicleSpeed=0.0, BatteryLevel=80.0, Location=SPRINGFIELD)]
    for i in range(6):
        out.append(pl(start_min + i, Gear="D", VehicleSpeed=30.0, Odometer=1000.0 + miles * i / 5, Location=(39.78 + 0.001 * i, -89.65)))
    out.append(pl(start_min + 6, Gear="P", VehicleSpeed=0.0, Odometer=1000.0 + miles))
    return out


def day():
    """Sleeping, then 60 minutes of a blind feed, then the feed back with the odometer it ended on."""
    return [sleeping(0), sleeping(5)] + [blind(m) for m in range(10, 70)] + [feed_row(70, odometer="1001.6"), feed_row(71, odometer="1001.6")]


def built():
    feed = day()
    until = T0 + timedelta(minutes=71)
    return feed, feed_gaps.stream_samples(stream_drive(), until)


def test_the_blind_stretch_is_found_and_a_short_one_is_not():
    feed = day()
    spans = feed_gaps.blind_spans(feed)
    assert spans == [(T0 + timedelta(minutes=10), T0 + timedelta(minutes=70))]
    short = [sleeping(0)] + [blind(m) for m in range(10, 15)] + [feed_row(16)]
    assert feed_gaps.blind_spans(short) == []
    assert feed_gaps.blind_spans([sleeping(0), sleeping(20)]) == [], "asleep is not blind"


def test_a_blind_stretch_still_open_at_the_end_counts():
    feed = [sleeping(0)] + [blind(m) for m in range(10, 30)]
    assert feed_gaps.blind_spans(feed) == [(T0 + timedelta(minutes=10), T0 + timedelta(minutes=29, microseconds=1))]


def test_without_the_stream_the_feed_loses_the_drive():
    feed, _ = built()
    assert replay(feed, new_modes=True, edges=True)["trips"] == []


def test_the_streams_drive_inside_a_blind_span_is_written_with_its_odometer_span():
    feed, stream = built()
    out = replay(feed_gaps.splice(feed, stream, feed_gaps.blind_spans(feed)), new_modes=True, edges=True)
    assert len(out["trips"]) == 1
    trip = out["trips"][0]
    assert trip["start_odometer"] == 1000.0 and abs(trip["destination_odometer"] - 1001.6) < 1e-6


def test_the_feed_stays_authoritative_outside_the_span():
    feed, stream = built()
    spans = feed_gaps.blind_spans(feed)
    merged = feed_gaps.splice(feed, stream, spans)
    outside = [s for s in merged if not any(a <= s["time"] < b for a, b in spans)]
    assert outside == [s for s in feed if not any(a <= s["time"] < b for a, b in spans)]
    assert merged == sorted(merged, key=lambda s: s["time"])
    # stream records before the span (minute 30 is inside; one at minute 5 is not) never leak in
    early = feed_gaps.stream_samples([pl(5, Odometer=5.0, Gear="D", VehicleSpeed=40.0)] + stream_drive(), T0 + timedelta(minutes=71))
    again = feed_gaps.splice(feed, early, spans)
    assert [s for s in again if s["time"] < spans[0][0]] == [s for s in feed if s["time"] < spans[0][0]]


def test_no_span_or_no_stream_returns_the_feed_unchanged():
    feed = [sleeping(0), feed_row(5), feed_row(6)]
    assert feed_gaps.splice(feed, [], []) is feed
    spans = [(T0 + timedelta(minutes=10), T0 + timedelta(minutes=70))]
    assert feed_gaps.splice(day(), [], spans) == day()


def test_a_drive_running_across_the_start_of_the_span_keeps_the_feeds_number():
    feed = [feed_row(0, driveNumber="7000", shift_state="D", carState="Driving", speed="30", odometer="999.5")] + [blind(m) for m in range(10, 30)] + [feed_row(30, odometer="1003")]
    moving = [pl(m, Gear="D", VehicleSpeed=30.0, Odometer=999.6 + 0.15 * m) for m in range(1, 20)]
    stream = feed_gaps.stream_samples(moving + [pl(20, Gear="P", VehicleSpeed=0.0, Odometer=1002.5)], T0 + timedelta(minutes=30))
    spans = feed_gaps.blind_spans(feed)
    merged = feed_gaps.splice(feed, stream, spans)
    inside = [s for s in merged if s["drive_no"] and spans[0][0] <= s["time"] < spans[0][1]]
    assert inside and {s["drive_no"] for s in inside} == {7000}, "the drive the feed lost sight of is the same drive"
    assert len(replay(merged, new_modes=True, edges=True)["trips"]) == 1


class RawConn:
    def __init__(self, payloads, fail=False):
        self.payloads, self.fail, self.sql = payloads, fail, []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if self.fail:
            raise RuntimeError("stream table gone")
        self.rows = [(p,) for p in self.payloads]
        return self

    def fetchall(self):
        return self.rows


def test_the_pollers_rebuild_reads_the_stream_only_when_the_feed_was_blind():
    from teslafi_live import TeslaFiPoller

    now = T0 + timedelta(minutes=72)
    quiet = RawConn(stream_drive())
    clear = [sleeping(0), feed_row(5), feed_row(6)]
    assert TeslaFiPoller.fill_blind(quiet, ALPHA, clear, now) == (clear, 0)
    assert quiet.sql == [], "nothing new on the usual path"
    conn = RawConn(stream_drive())
    merged, gaps = TeslaFiPoller.fill_blind(conn, ALPHA, day(), now)
    assert gaps == 1 and len(replay(merged, new_modes=True, edges=True)["trips"]) == 1
    assert TeslaFiPoller.fill_blind(RawConn([], fail=True), ALPHA, day(), now) == (day(), 0), "a stream that cannot be read leaves the feed alone"


def test_a_drive_the_feed_renumbers_mid_stretch_is_one_drive():
    """The feed's number stepped from 7146 to 7147 between two driving rows a minute apart (shift D both):
    the second half, 0.31 mi, was left out of the first drive and then dropped as a one-row drive."""
    rows = [
        feed_row(0, carState="Idling", odometer="63412.9", shift_state="P", speed="0"),
        feed_row(1, carState="Driving", odometer="63412.9", shift_state="R", speed="1", driveNumber="7146"),
        feed_row(2, carState="Driving", odometer="63412.97", shift_state="D", speed="21", driveNumber="7146"),
        feed_row(3, carState="Driving", odometer="63413.44", shift_state="D", speed="14", driveNumber="7146"),
        feed_row(3.99, carState="Driving", odometer="63413.75", shift_state="D", speed="0", driveNumber="7147"),
        feed_row(5, carState="Idling", odometer="63413.76", shift_state="", speed=""),
    ]
    trips = replay(rows, new_modes=True, edges=True)["trips"]
    assert len(trips) == 1 and trips[0]["destination_odometer"] >= 63413.75
    # a poll in park between the two numbers ends the first drive where it was
    rows.insert(4, feed_row(3.5, carState="Idling", odometer="63413.44", shift_state="P"))
    assert replay(rows, new_modes=True, edges=True)["trips"][0]["destination_odometer"] == 63413.44

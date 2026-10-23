"""Top routes and destinations: both ways folded into one route, named the way it is mostly
driven, the auto-tag that covers it, and the quickest hour to leave only when enough drives
say so."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import toproutes

TZ = ZoneInfo("America/Chicago")
HOME, WORK, LAKE = {"id": 1, "name": "Home"}, {"id": 2, "name": "Work"}, {"id": 3, "name": "Lake"}
T0 = datetime(2026, 3, 2, 13, 0, tzinfo=timezone.utc)  # 7 AM in Springfield


def d(a, b, day, minutes=12, hour=0, kwh=1.5, miles=5.0):
    start = T0 + timedelta(days=day, hours=hour)
    return {"start_place": a, "end_place": b, "start_date": start, "destination_date": start + timedelta(minutes=minutes), "kwh_used": kwh, "miles": miles}


def test_both_ways_are_one_route_named_the_way_it_is_mostly_driven():
    drives = [d(HOME, WORK, i) for i in range(5)] + [d(WORK, HOME, i, minutes=10, hour=10) for i in range(3)] + [d(LAKE, HOME, 9)]
    (r,) = toproutes.routes(drives, [], TZ)
    assert (r["a"]["name"], r["b"]["name"], r["n"], r["ways"]) == ("Home", "Work", 8, [("Home", "Work", 5), ("Work", "Home", 3)]), "a route driven once is not listed"
    assert r["time"] == timedelta(minutes=12) and r["kwh"] == 1.5 and r["wh_per_mi"] == 300.0 and r["first"] == T0
    assert r["last"] == T0 + timedelta(days=4), "the latest drive either way"


def test_a_loop_is_its_own_route_and_a_rule_gives_its_chip():
    rules = [{"from_name": "Home", "to_name": "Work", "tag": "Commute", "both_ways": True}, {"from_name": "Home", "to_name": "Home", "tag": "Sunday drive", "both_ways": True}]
    drives = [d(WORK, HOME, i) for i in range(3)] + [d(HOME, HOME, i, minutes=50) for i in range(2)]
    tags = {(r["a"]["name"], r["b"]["name"], r["loop"]): r["tag"] for r in toproutes.routes(drives, rules, TZ)}
    assert tags == {("Work", "Home", False): "Commute", ("Home", "Home", True): "Sunday drive"}


def test_the_quickest_hour_needs_enough_drives_and_a_real_saving():
    early = [d(HOME, WORK, i, minutes=11) for i in range(5)]
    late = [d(HOME, WORK, i, minutes=16, hour=2) for i in range(5)]  # the same days: a week later the clocks change
    (r,) = toproutes.routes(early + late, [], TZ)
    (q,) = r["quick"]
    assert (q["from"], q["hour"], q["minutes"], q["n"]) == ("Home", 7, 11, 5) and q["typical"] == 13.5
    (r,) = toproutes.routes(early + late[:4], [], TZ)
    assert r["quick"] == [], "four drives in the later hour are not enough to compare"
    close = [d(HOME, WORK, i, minutes=12) for i in range(5)] + [d(HOME, WORK, i, minutes=13, hour=2) for i in range(5)]
    (r,) = toproutes.routes(close, [], TZ)
    assert r["quick"] == [], "a minute in thirteen is the traffic, not the hour"


def test_destinations_count_arrivals_and_say_when_last():
    drives = [d(HOME, WORK, i) for i in range(3)] + [d(WORK, HOME, 5)] + [{**d(HOME, None, 7)}]
    out = toproutes.destinations(drives)
    assert [(x["place"]["name"], x["n"]) for x in out] == [("Work", 3), ("Home", 1)] and out[0]["share"] == 1.0
    assert out[0]["last"] == T0 + timedelta(days=2, minutes=12)


def test_a_route_carries_its_key_and_its_drives_ids():
    drives = [dict(d(HOME, WORK, i), id=10 + i) for i in range(3)] + [dict(d(WORK, HOME, 4), id=20)]
    (r,) = toproutes.routes(drives, [], TZ)
    assert r["key"] == "1-2" == toproutes.route_key(2, 1) and sorted(r["ids"]) == [10, 11, 12, 20]
    (loop,) = toproutes.routes([dict(d(LAKE, LAKE, i), id=i) for i in range(2)], [], TZ)
    assert loop["key"] == "3-3"

"""Every map that draws a drive draws its matched line when there is one (routematch), the
car's own readings otherwise; the drive page can lay the readings over the line."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import routematch as R
from test_empty import bare, client  # noqa: F401 - fixtures
from thumbs import route_svg

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
END = T0 + timedelta(minutes=10)
RAW = [(T0 + timedelta(seconds=45 * i), 39.80 + 0.001 * i, -89.65) for i in range(10)]
ROADS = [[39.80, -89.65], [39.802, -89.6512], [39.805, -89.6508], [39.809, -89.65]]  # the simplified line
FULL = [ROADS[0], [39.801, -89.6506], *ROADS[1:]]  # the whole one, for the drive page alone
THUMB = [ROADS[0], ROADS[2], ROADS[3]]


class Conn:
    """Answers by what is asked: the drive's readings, its matched line (whichever of the
    three stored forms the query names), its trip."""

    def __init__(self, matched=True, trip_end=END, raw=RAW, trips=None, routes=None):
        self.asked, self.matched, self.trip_end, self.raw = [], matched, trip_end, raw
        self.trips = trips or [{"start_date": T0, "destination_date": END}]
        self.routes = routes

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.asked.append(s)
        if "FROM cc_trip_routes" in s:
            form = THUMB if "geometry_thumb" in s else ROADS if "geometry_simple" in s else FULL
            if self.routes is not None:
                rows = self.routes(s)
            else:
                rows = [{"start_date": T0, "trip_end": self.trip_end, "line": R.encode(form)}] if self.matched else []
        elif "FROM cc_positions" in s:
            rows = [{"at": a, "latitude": la, "longitude": lo} for a, la, lo in self.raw]
        elif s.startswith("SELECT vin, start_date, destination_date"):
            rows = [{"vin": "V1", "start_date": T0, "destination_date": END, "arrived": True}]
        elif "FROM cc_drive_tags" in s:
            rows = [{"trip_id": 7}]
        elif "FROM trips" in s:
            rows = self.trips
        else:
            rows = []
        return type("R", (), {"fetchall": lambda _s: rows, "fetchone": lambda _s: rows[0] if rows else None})()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_timeline_thumbnails_draw_the_stored_thumbnail_line(bare, monkeypatch):  # noqa: F811
    trips = [{"id": 7, "vin": "V1", "start_date": T0, "destination_date": END}]
    conn = Conn()
    monkeypatch.setattr(bare, "db", lambda *a, **k: conn)
    assert bare.route_thumbs(trips, T0, END) == {7: route_svg(THUMB, 96, 48)}
    assert any("geometry_thumb" in q for q in conn.asked), "the small line made at match time, not the whole one"
    monkeypatch.setattr(bare, "db", lambda *a, **k: Conn(matched=False))
    raw = bare.route_thumbs(trips, T0, END)[7]
    assert raw and raw != route_svg(THUMB, 96, 48), "no match: the readings, as before"
    monkeypatch.setattr(bare, "db", lambda *a, **k: Conn(trip_end=END + timedelta(minutes=3)))
    assert bare.route_thumbs(trips, T0, END)[7] == raw, "a drive changed since its match draws its readings"


def _month(bare, monkeypatch, conn):  # noqa: F811
    monkeypatch.setattr(bare, "db", lambda *a, **k: conn)
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha", "live": True}])
    monkeypatch.setattr(bare, "pick_vin", lambda v: "V1")


def test_the_month_map_draws_the_matched_drive_once(bare, client, monkeypatch):  # noqa: F811
    _month(bare, monkeypatch, Conn())
    lines = client.get("/api/month-routes?month=2026-09&vin=V1").json()
    assert [ln["pts"] for ln in lines] == [ROADS], "the simplified roads, and not its readings a second time"
    _month(bare, monkeypatch, Conn(matched=False))
    lines = client.get("/api/month-routes?month=2026-09&vin=V1").json()
    assert len(lines) == 1 and lines[0]["pts"][0] == [39.8, -89.65] and len(lines[0]["pts"]) == 5, "unmatched: the readings, thinned as before"


def test_the_month_map_never_draws_a_chord_across_a_matched_drive(bare, client, monkeypatch):  # noqa: F811
    """Readings just before a matched drive and just after it are under ten minutes apart;
    joined, they would cut straight across the matched line."""
    before = [(T0 - timedelta(seconds=60 * (3 - i)), 39.79 + 0.001 * i, -89.66) for i in range(3)]
    inside = RAW[:3]
    after = [(END + timedelta(seconds=60 * (i + 1)), 39.81 + 0.001 * i, -89.64) for i in range(3)]
    _month(bare, monkeypatch, Conn(raw=before + inside + after))
    lines = [ln["pts"] for ln in client.get("/api/month-routes?month=2026-09&vin=V1").json()]
    assert lines[0] == ROADS and len(lines) == 3
    assert lines[1][0] == [39.79, -89.66] and lines[1][-1] == [39.792, -89.66], "the line before ends where the drive begins"
    assert lines[2][0] == [39.81, -89.64], "and a new one starts after it"


def test_a_months_map_of_two_hundred_matched_drives_stays_small(bare, client, monkeypatch):  # noqa: F811
    """What /api/month-routes sends for a busy month: a whole Valhalla line is a vertex every
    few metres; the stored simplified one keeps the turns. Measured, not guessed."""
    # a 12 km drive on a winding road, a vertex every 5 m: what a matched line looks like
    road, la, lo = [], 39.70, -89.70
    for i in range(2400):
        la += 0.000045 * (1 if (i // 40) % 2 == 0 else 0.3)
        lo += 0.000045 * (0.3 if (i // 40) % 2 == 0 else 1)
        road.append([la, lo])
    forms = R.geometries(road)
    trips = [{"start_date": T0 - timedelta(hours=3 * k), "destination_date": T0 - timedelta(hours=3 * k) + timedelta(minutes=20)} for k in range(200)]

    def routes(sql):
        col = "geometry_simple" if "geometry_simple" in sql else "geometry"
        return [{"start_date": t["start_date"], "trip_end": t["destination_date"], "line": forms[col]} for t in trips]

    _month(bare, monkeypatch, Conn(raw=[], trips=trips, routes=routes))
    after = len(client.get("/api/month-routes?month=2026-09&vin=V1").content)
    whole = len(R.decode(forms["geometry"]))
    before = len(str([[[round(a, 4), round(b, 4)] for a, b in R.decode(forms["geometry"])]] * 200))  # the whole line, as first shipped
    assert len(R.decode(forms["geometry_simple"])) < whole / 10
    assert after < 300_000, after  # about 1.2 KB a drive, for a drive that turns every 200 m
    assert before > 8 * after, (before, after)


def test_a_places_traces_follow_the_roads_when_matched(bare, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "places_detail", lambda: [{"id": 5, "latitude": 39.805, "longitude": -89.65, "radius_m": 150.0}])
    conn = Conn()
    monkeypatch.setattr(bare, "db", lambda *a, **k: conn)
    assert client.get("/places/5/around").json() == {"traces": [ROADS]}
    assert not any("FROM cc_positions" in s for s in conn.asked), "the matched line stands in for the readings"


def test_the_tag_map_reads_each_cars_matched_lines_once_and_no_positions_for_them(bare, client, monkeypatch):  # noqa: F811
    from fastapi.responses import HTMLResponse

    ds = [{"id": 7, "vin": "V1", "start_date": T0, "destination_date": END, "miles": 6.0, "duration": END - T0}]
    conn = Conn()
    seen = {}
    monkeypatch.setattr(bare, "db", lambda *a, **k: conn)
    monkeypatch.setattr(bare, "pick", lambda r: {"vin": "V1", "name": "Alpha"})
    monkeypatch.setattr(bare, "tag_names", lambda: ["Trip"])
    monkeypatch.setattr(bare, "drives", lambda *a, **k: ds)
    monkeypatch.setattr(bare, "charges", lambda *a, **k: [])
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha", "live": True}])
    monkeypatch.setattr(bare, "page", lambda request, name, **kw: seen.update(kw) or HTMLResponse(""))
    assert client.get("/tags?tag=Trip&all=1").status_code == 200
    assert seen["routes"] == [{"c": 1, "pts": ROADS}]
    assert sum("FROM cc_trip_routes" in q for q in conn.asked) == 1 and not any("FROM cc_positions" in q for q in conn.asked)


def test_day_page_and_drive_page_choose_the_matched_line():
    src = (Path(__file__).parent / "app.py").read_text()
    day = src[src.index("def day_for_car(") : src.index('@site.get("/day"')]
    assert "routematch.matched_lines(conn, vin, lookback, end, \"simple\")" in day and "routematch.choose(" in day
    drive = src[src.index("def drive_detail(") : src.index("def detail_cost(")]
    assert 'routematch.matched_line(conn, car["vin"], trip["start_date"], trip["destination_date"], "full")' in drive, "the drive page alone reads the whole line"
    assert "route = routematch.choose(readings, matched)" in drive
    assert 'stats["max_mph"] = trip.get("max_mph")' in drive and "trip_stats(trip[\"miles\"]" in drive, "figures stay the car's: odometer miles, not the line"


def _drive_html(bare, **kw):  # noqa: F811
    t0 = T0
    env = bare.templates.env
    trip = {
        "id": 1,
        "start_date": t0,
        "destination_date": END,
        "start_odometer": 10.0,
        "destination_odometer": 12.0,
        "miles": 2.0,
        "duration": END - t0,
        "from": "Elm Street, Springfield",
        "to": "Main Street, Springfield",
        "from_named": True,
        "to_named": True,
        "temp_f": None,
        "by_hand": False,
    }
    chart = {
        "t0": 0,
        "t1": 600,
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 70.0], [600, 69.0]]},
        "extras": [],
        "lane": [],
        "events": [],
        "ticks": [],
    }
    car = {"vin": "V1", "name": "Alpha", "live": True, "capacity_kwh": 80}
    return env.get_template("drive.html").render(
        root="/cc",
        now=t0,
        car=car,
        active="",
        vehicles=[car],
        q="",
        colors={"V1": 0},
        names={"V1": "Alpha"},
        cur="$",
        trip=trip,
        pos={"start_position_latitude": 39.8, "start_position_longitude": -89.65, "destination_position_latitude": 39.809, "destination_position_longitude": -89.65},
        stats={},
        lvl_start=None,
        lvl_end=None,
        tags=[],
        chart=chart,
        hidden=False,
        samples=[],
        every=1,
        fixes=0,
        **kw,
    )


def test_the_drive_page_offers_the_cars_own_readings_over_a_matched_line(bare):  # noqa: F811
    html = _drive_html(bare, route=ROADS, matched=True, readings=[[39.80, -89.65], [39.809, -89.65]])
    assert "The car's own readings" in html and 'id="show-readings"' in html
    assert "[[39.8, -89.65], [39.809, -89.65]]" in html, "the readings go to the page for the overlay"
    html = _drive_html(bare, route=[[39.80, -89.65], [39.809, -89.65]])
    assert "The car's own readings" not in html, "unmatched: the line already is the readings"

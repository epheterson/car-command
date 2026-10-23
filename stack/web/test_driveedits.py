"""Fixing a drive by hand: the pure rules (when two drives merge, where a cut can fall, which
readings look wrong), the routes that carry them out, and that the lists leave out what a merge took in."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import driveedits
from test_empty import bare, client  # noqa: F401 - fixtures

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
VIN = "5YJ3E1EA0JF000001"


def trip(start, minutes, **kw):
    return {"start_date": start, "destination_date": start + minutes * M if minutes else None, **kw}


def test_two_drives_merge_when_the_stop_between_them_was_short():
    a, b = trip(T0, 20), trip(T0 + 35 * M, 10)
    assert driveedits.merge_refusal(a, b, charged=False) is None
    assert driveedits.merge_refusal(a, trip(T0 + 50 * M, 10), False) is None, "thirty minutes exactly is still a pause"
    assert "half an hour" in driveedits.merge_refusal(a, trip(T0 + 51 * M, 10), False)


@pytest.mark.parametrize(
    "a, b, charged, why",
    [
        (trip(T0, 0), trip(T0 + 30 * M, 10), False, "still going"),
        (trip(T0, 20), None, False, "no drive after"),
        (trip(T0, 20), trip(T0 + 30 * M, 0), False, "still going"),
        (trip(T0, 20), trip(T0 + 10 * M, 5), False, "starts before"),
        (trip(T0, 20), trip(T0 + 30 * M, 10), True, "charged"),
    ],
)
def test_a_merge_says_why_it_cannot_be_made(a, b, charged, why):
    assert why in driveedits.merge_refusal(a, b, charged)


def fix(minute, odo=None, **kw):
    return {"at": T0 + minute * M, "odometer": odo, "latitude": 39.0, "longitude": -89.0, "elevation": 180.0, **kw}


def test_a_cut_falls_on_the_nearest_reading_inside_the_drive_that_was_not_left_out():
    fixes = [fix(0, 100.0), fix(10, 105.0), fix(20, 110.0, excluded=True), fix(30, 115.0), fix(40, 120.0)]
    start, end = T0, T0 + 40 * M
    cut = driveedits.cut_point(fixes, start, end, T0 + 21 * M)
    assert cut["at"] == T0 + 30 * M and cut["odometer"] == 115.0, "the reading left out is not a place to cut"
    assert driveedits.cut_point(fixes, start, end, T0 - 5 * M)["at"] == T0 + 10 * M, "before the drive picks its first reading inside it"
    assert driveedits.cut_point(fixes, start, end, T0 + 99 * M)["at"] == T0 + 30 * M, "and after it the last one"


def test_a_cut_without_an_odometer_takes_the_line_between_the_readings_either_side():
    fixes = [fix(0, 100.0), fix(10, None), fix(20, 110.0)]
    assert driveedits.cut_point(fixes, T0, T0 + 30 * M, T0 + 10 * M)["odometer"] == pytest.approx(105.0)
    assert "no odometer" in driveedits.cut_point([fix(0, None), fix(10, None)], T0 - M, T0 + 30 * M, T0 + 10 * M)
    assert "no reading" in driveedits.cut_point([], T0, T0 + 30 * M, T0 + 10 * M)
    assert "no reading" in driveedits.cut_point([fix(0, 100.0), fix(40, 120.0)], T0, T0 + 40 * M, T0 + 20 * M), "the two ends are the drive's own"


def test_a_position_or_a_height_far_from_the_readings_either_side_looks_wrong():
    moving = [fix(m, 100.0 + m, latitude=39.0 + 0.001 * m, longitude=-89.0) for m in range(8)]
    assert driveedits.suspects(moving) == set()
    jumped = [dict(f) for f in moving]
    jumped[4]["latitude"] += 0.5  # 55 km away and back within a minute
    assert driveedits.suspects(jumped) == {jumped[4]["at"]}
    dug = [dict(f) for f in moving]
    dug[3]["elevation"] = -4000.0
    assert driveedits.suspects(dug) == {dug[3]["at"]}
    dug[3]["excluded"] = True
    assert driveedits.suspects(dug) == set(), "a reading already left out is not looked at again"
    slope = [fix(m, 100.0 + m, elevation=180.0 + 40 * m) for m in range(8)]
    assert driveedits.suspects(slope) == set(), "a steady climb is a hill"
    assert driveedits.suspects(moving[:2]) == set()


def test_rows_that_could_be_fixed_are_never_among_those_the_table_skips():
    every = [{"at": T0 + m * M} for m in range(10)]
    shown = every[::4]
    flagged = {every[5]["at"], every[8]["at"]}
    got = driveedits.with_flagged(shown, every, flagged)
    assert [r["at"] for r in got] == sorted({*[r["at"] for r in shown], *flagged})
    assert driveedits.with_flagged(shown, every, set()) is shown


def test_a_moment_is_read_from_a_reading_or_the_chart():
    at = T0.replace(microsecond=123456)
    assert driveedits.parse_at(at.isoformat()) == at
    assert driveedits.parse_at(str(T0.timestamp())) == T0
    assert driveedits.parse_at("2026-10-05T08:00:00") == T0, "no zone is the zone the readings are kept in"
    assert driveedits.parse_at("") is None and driveedits.parse_at("soon") is None and driveedits.parse_at(None) is None


def test_the_lists_and_totals_leave_out_a_drive_a_merge_took_in():
    app_py = (Path(__file__).parent / "app.py").read_text()
    assert "driveedits.ABSORBED" in app_py[app_py.index("HIDDEN_DRIVE = ") :][:300], "moved_sql rides the hidden clause, so every list, total and arrow follows"
    for fn in ("def place_arrivals", "def matched_spans"):
        assert "driveedits.ABSORBED" in app_py[app_py.index(fn) :][:1800], fn
    assert "FROM trips t WHERE vin = %s AND start_date >= %s AND start_date <= %s\"\"\" + driveedits.ABSORBED" in app_py, "the miles since the last charge"
    assert "NOT EXISTS (SELECT 1 FROM cc_drive_excluded" in driveedits.left_out() and "p.vin = x.vin" not in driveedits.left_out()


def test_the_drive_page_draws_from_the_readings_not_left_out():
    src = (Path(__file__).parent / "app.py").read_text()
    page = src[src.index("def drive_detail") : src.index("def parked_detail")]
    assert "track = [r for r in every_fix if not r[\"excluded\"]]" in page and "driveedits.left_out()" in page, "the climb, route, speeds and coverage skip them"
    assert "drive_samples(\n        every_fix," in page, "the table keeps them, struck through, so each can be taken back"
    facts_py = (Path(__file__).parent / "facts.py").read_text()
    assert facts_py.count("driveedits.left_out(") >= 2, "the top speed and the climb behind the page's figures"


# ---- the routes ----


@pytest.fixture
def edits(bare, monkeypatch):  # noqa: F811
    """The routes with the edits themselves recorded, for a drive 7 on one car and no other."""
    calls = []
    monkeypatch.setattr(driveedits, "vin_of", lambda conn, tid: VIN if tid in (7, 8) else None)
    monkeypatch.setattr(driveedits, "merge", lambda conn, tid, where, partner=None: calls.append(("merge", tid, partner)) or None)
    monkeypatch.setattr(driveedits, "split", lambda conn, tid, at: calls.append(("split", tid, at)) or None)
    monkeypatch.setattr(driveedits, "leave_out", lambda conn, tid, at: calls.append(("exclude", tid, at)) or None)
    monkeypatch.setattr(driveedits, "take_back", lambda conn, tid, at: calls.append(("include", tid, at)) or None)
    monkeypatch.setattr(driveedits, "undo", lambda conn, edit, tid: calls.append(("undo", edit, tid)) or (None, 8))
    monkeypatch.setattr(bare, "fill_trip_facts", lambda conn, vin: calls.append(("facts", vin)))
    bare.calls = calls
    return bare


def post(client, bare, path, **data):  # noqa: F811
    import auth

    return client.post(path, data={**data, "csrf_token": auth.csrf_token(bare.AUTH["secret"], None)})


def test_each_edit_lands_back_on_the_drive_and_works_the_facts_out_again(edits, client):  # noqa: F811
    edits._cache["today"] = (datetime.now(timezone.utc), "stale")
    r = post(client, edits, "/drives/7/merge", vin=VIN, partner=T0.isoformat())
    assert r.status_code == 303 and r.headers["location"] == f"/drives/7?vin={VIN}"
    assert edits.calls == [("merge", 7, T0), ("facts", VIN)] and "today" not in edits._cache, "the totals read the new drives at once"
    at = T0.replace(microsecond=500000).isoformat()
    for act in ("split", "exclude", "include"):
        edits.calls.clear()
        r = post(client, edits, f"/drives/7/{act}", vin=VIN, at=at)
        assert r.status_code == 303 and r.headers["location"] == f"/drives/7?vin={VIN}"
        assert edits.calls[0] == (act, 7, T0.replace(microsecond=500000)) and edits.calls[-1] == ("facts", VIN)


def test_undo_goes_to_the_drive_that_is_left(edits, client):  # noqa: F811
    r = post(client, edits, "/drives/7/undo", vin=VIN, edit="31")
    assert r.status_code == 303 and r.headers["location"] == f"/drives/8?vin={VIN}", "undoing a cut from the half it wrote leaves that half gone"
    assert edits.calls[0] == ("undo", 31, 7)


def test_a_refusal_comes_back_as_a_line_on_the_drive(edits, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(driveedits, "merge", lambda conn, tid, where, partner=None: "The car charged between the two.")
    r = post(client, edits, "/drives/7/merge", vin=VIN)
    assert r.status_code == 303 and r.headers["location"] == f"/drives/7?vin={VIN}&said=charged"
    monkeypatch.setattr(driveedits, "undo", lambda conn, edit, tid: (driveedits.MSG["later"], None))
    r = post(client, edits, "/drives/7/undo", vin="", edit="31")
    assert r.headers["location"].startswith("/drives/7?said=later"), "no car in the form, no vin in the address"
    assert post(client, edits, "/drives/7/split", vin=VIN, at="").headers["location"].endswith("said=pickcut")


@pytest.mark.parametrize("act", ["merge", "split", "exclude", "include", "undo"])
def test_a_drive_that_is_not_there_is_a_404(edits, client, act):  # noqa: F811
    r = post(client, edits, f"/drives/999/{act}", vin=VIN, at=T0.isoformat(), edit="1")
    assert r.status_code == 404
    assert not edits.calls, "nothing was changed"


@pytest.mark.parametrize("act", ["merge", "split", "exclude", "include", "undo"])
def test_an_edit_without_the_forms_token_is_refused(edits, client, act):  # noqa: F811
    import auth

    edits.AUTH.update(enabled=True, secret=auth.new_secret(), password=auth.hash_password("a good long password", n=2**10))
    client.cookies.set(auth.SESSION_COOKIE, auth.new_session(edits.AUTH["secret"]))
    r = client.post(f"/drives/7/{act}", data={"vin": VIN, "at": T0.isoformat(), "edit": "1"})
    assert r.status_code == 400 and "stale" in r.text
    assert not edits.calls


def render_drive(**over):
    import jinja2

    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    car = {"name": "Alpha", "model": "Example EV", "updated_age": "just now", "capacity_kwh": 117.2, "vin": VIN, "live": True}
    trip = {
        "id": 7,
        "start_date": T0,
        "destination_date": T0 + 40 * M,
        "start_odometer": 100.0,
        "destination_odometer": 126.0,
        "miles": 26.0,
        "duration": 40 * M,
        "from": "Alpha Park",
        "to": "Bravo Mall",
        "by_hand": False,
    }
    chart = {
        "t0": 0,
        "t1": 2400,
        "primary": {"key": "level", "label": "Battery", "unit": "%", "lo": 0, "hi": 100, "points": [[0, 74.0], [2400, 65.0]]},
        "extras": [],
        "lane": [[0, 2400, "driving"]],
        "events": [],
        "ticks": [],
        "tz": "America/Chicago",
    }
    row = {
        "at": T0 + 10 * M,
        "speed": 30.0,
        "level": 70.0,
        "rated": 200.0,
        "odometer": 110.0,
        "elevation": 180.0,
        "outside": None,
        "inside": None,
        "power_kw": 12.0,
        "excluded": False,
        "suspect": False,
    }
    ctx = dict(
        root="/cc",
        now=T0,
        car=car,
        active="drives",
        vehicles=[],
        q="",
        colors={VIN: 0},
        names={VIN: "Alpha"},
        cur="$",
        csrf="tok",
        trip=trip,
        pos=None,
        stats={},
        lvl_start=74.0,
        lvl_end=65.0,
        route=[],
        tags=[],
        chart=chart,
        hidden=False,
        every=1,
        fixes=3,
        samples=[row, {**row, "at": T0 + 20 * M, "speed": 190.0, "suspect": True}, {**row, "at": T0 + 30 * M, "excluded": True}],
    )
    return env.get_template("drive.html").render(**{**ctx, **over})


def test_the_drive_page_offers_the_edits_with_the_forms_token():
    html = render_drive(
        left_out=1,
        suspects=1,
        cut_ats=[T0.timestamp() + 600, T0.timestamp() + 1200],
        merge_offer={"start": T0 + 32 * M, "gap": 12 * M, "miles": 3.4},
        last_edit={"id": 31, "kind": "split", "b_start": T0 + 10 * M, "made_here": False, "loses": "Gym"},
        waiting=[{"id": 40, "kind": "merge", "a_start": T0}],
        said=driveedits.MSG["charged"],
    )
    assert "The car charged between the two." in html
    for act in ("merge", "split", "undo", "exclude", "include"):
        assert f'action="/cc/drives/7/{act}"' in html, act
    assert html.count('name="csrf_token" value="tok"') >= 6
    assert "The next drive left 12 min after this one arrived" in html and "3.4 mi more" in html
    assert 'name="edit" value="31"' in html and "Cut at " in html
    assert "Undoing drops the second half" in html and "Gym" in html, "an Undo that would drop a tag asks first and names it"
    assert 'name="edit" value="40"' in html and "is waiting" in html, "a change the history no longer holds can still be undone"
    assert "1 left out" in html and "1 look wrong" in html
    assert 'class="btn sm drv-arm"' in html, "the chart is armed from a Split… button"
    assert f'class="pickable" tabindex="0" data-at="{(T0 + 10 * M).isoformat()}"' in html, "a tap on a row picks it"
    assert f'form="rd-include" name="at" value="{(T0 + 30 * M).isoformat()}"' in html and 'class="out"' in html
    assert 'class="pickable odd"' in html and f'form="rd-exclude" name="at" value="{(T0 + 20 * M).isoformat()}"' in html, "a reading that looks wrong has its button in place"
    assert html.count('form="rd-exclude"') == 1 and "form=\"rd-split\"" not in html, "no button on an ordinary row"
    assert "cc:chart-pick" in html and 'class="drv-pick"' in html and "data-pick-out" in html
    assert f'name="partner" value="{(T0 + 32 * M).isoformat()}"' in html, "the merge form says which drive it offered"
    assert html.count("data-hold") >= 5 and "data-hold" in (Path(__file__).parent / "static" / "shell.js").read_text(), "a second tap on any of them is not sent"


def test_the_drive_page_offers_nothing_to_fix_on_a_drive_still_going_or_untouched():
    html = render_drive(
        trip={
            "id": 7,
            "start_date": T0,
            "destination_date": None,
            "start_odometer": 100.0,
            "destination_odometer": None,
            "miles": None,
            "duration": None,
            "from": "Alpha Park",
            "to": "In progress",
            "by_hand": False,
        }
    )
    assert "/merge" not in html and "/undo" not in html and 'class="btn sm drv-arm"' not in html
    assert "Split here" not in html, "a drive under way has readings to leave out but nowhere to cut yet"
    assert "left out" not in html.split("Every reading")[1].split("</h2>")[0]
    made = render_drive(last_edit={"id": 4, "kind": "split", "b_start": T0, "made_here": True, "loses": None})
    assert "The rest of a drive cut at" in made and "Undoing drops" not in made
    merged = render_drive(last_edit={"id": 5, "kind": "merge", "b_start": T0, "made_here": False, "loses": None})
    assert "Merged with the drive after it" in merged


def test_a_merge_without_the_partner_the_page_offered_is_stale(edits, client):  # noqa: F811
    r = post(client, edits, "/drives/7/merge", vin=VIN)
    assert r.status_code == 303 and edits.calls[0] == ("merge", 7, None), "the form always sends it; an old page sends nothing"


def test_a_refusal_is_told_by_a_short_code_and_every_code_has_its_words():
    assert len(driveedits.CODES) == len(driveedits.MSG), "no two refusals share a sentence"
    for code, words in driveedits.MSG.items():
        assert driveedits.CODES[words] == code
    assert driveedits.merge_refusal(trip(T0, 20), trip(T0 + 30 * M, 5), False, now=T0 + 36 * M) == driveedits.MSG["fresh"], "a drive that ended minutes ago waits"
    assert driveedits.merge_refusal(trip(T0, 20), trip(T0 + 30 * M, 5), False, now=T0 + 45 * M) is None


def test_the_page_says_only_what_a_known_code_says(edits, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(edits, "drive", lambda vin, tid: None)
    assert client.get("/drives/7?said=<script>alert(1)</script>").status_code == 404
    assert render_drive(said="").count('class="said"') == 0


def test_a_drive_a_merge_took_in_sends_you_to_the_one_that_has_it(edits, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(edits, "drive", lambda vin, tid: {"id": tid, "vin": VIN})
    monkeypatch.setattr(driveedits, "absorbed_by", lambda conn, tid: 5)
    r = client.get(f"/drives/7?vin={VIN}")
    assert r.status_code == 303 and r.headers["location"] == f"/drives/5?vin={VIN}"


def test_a_hand_made_drive_with_an_edit_on_it_is_not_removed(edits, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(driveedits, "involved", lambda conn, tid: True)
    r = post(client, edits, "/drives/7/remove", vin=VIN)
    assert r.status_code == 303 and r.headers["location"] == f"/drives/7?said=stands&vin={VIN}"


def test_every_reader_of_a_drives_readings_skips_those_left_out():
    src = (Path(__file__).parent / "app.py").read_text()
    for marker in ("def route_positions", "def drive_detail", "def route_thumbs", "def day_sections"):
        if marker in src:
            assert "driveedits.left_out(" in src[src.index(marker) :][:6000], marker
    assert src.count("driveedits.left_out(") >= 7, "the day track, the matcher's input, thumbnails, place traces and the drive page"
    assert "FROM cc_positions WHERE vin = %s AND at BETWEEN %s AND %s ORDER BY at" not in src, "a reader of a drive's window that forgot the clause"
    for mod in ("health.py", "tesla_compare.py"):
        assert "driveedits.ABSORBED" in (Path(__file__).parent / mod).read_text(), mod


def test_a_click_on_the_chart_picks_a_moment():
    assert 'addEventListener("click"' in (Path(__file__).parent / "static" / "chart.js").read_text()


def test_the_merge_offer_built_from_rows_names_the_partner_the_page_posts(monkeypatch):
    for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    import app

    trip = {"id": 7, "vin": "ALPHA", "start_date": T0, "destination_date": T0 + 20 * M}
    nxt = {"id": 8, "start_date": T0 + 32 * M, "destination_date": T0 + 50 * M, "start_odometer": 100.0, "destination_odometer": 103.4}
    monkeypatch.setattr(driveedits, "merge_partner", lambda conn, t, moved: (nxt, None))
    monkeypatch.setattr(driveedits, "edits_for", lambda conn, i: [])
    monkeypatch.setattr(driveedits, "waiting_for", lambda conn, t: [])
    offers = app.drive_edit_offers(None, trip)
    assert offers["merge_offer"]["start"] == nxt["start_date"], "the merge form posts the partner's start; without it the page fails"
    html = render_drive(**offers)
    assert f'name="partner" value="{nxt["start_date"].isoformat()}"' in html

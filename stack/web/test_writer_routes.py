"""The writer choice on the pages: offered where a car has more than one source, a switch
handed to history_writer in a thread, and the comparison page reading only what is kept."""

from datetime import datetime, timedelta, timezone

import pytest

import history_writer
import writerswitch
from test_empty import bare, client  # noqa: F401 - fixtures

ALPHA = "5YJ3E1EA0JF000001"
BRAVO = "5YJ3E1EA0JF000002"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)


def form(bare, data):  # noqa: F811
    import auth

    return dict(data, csrf_token=auth.csrf_token(bare.AUTH["secret"], None))


@pytest.fixture
def two_teslas(bare, monkeypatch):  # noqa: F811
    """Alpha on the TeslaFi feed and the stream; Bravo on the feed alone."""
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": ALPHA, "name": "Alpha", "live": False}, {"vin": BRAVO, "name": "Bravo", "live": False}])
    monkeypatch.setattr(bare.teslafi, "vin_for_token", {"a": ALPHA, "b": BRAVO})
    monkeypatch.setattr(bare.tesla_stream_worker, "vins", {ALPHA})
    monkeypatch.setattr(bare, "live_vehicle", lambda vin: {"vin": vin or ALPHA, "name": "Alpha", "model": "Model 3", "updated_age": "now", "live": False})
    switched, jobs = [], []

    def switch(conn, vin, new, current, now, tz, last_ok=None, teslamate_spans=None):
        switched.append((vin, new, current))
        return T0, None

    monkeypatch.setattr(history_writer, "switch", switch)
    monkeypatch.setattr(bare, "switcher", writerswitch.Switcher(connect=bare.db, tz=timezone.utc, after=bare.after_switch, spawn=jobs.append, clock=lambda: T0))
    bare.switched, bare.jobs = switched, jobs
    return bare


def test_with_no_choice_kept_the_writer_is_what_it_always_was(two_teslas):
    assert two_teslas.source_of({"vin": ALPHA}) == "feed"
    opts = two_teslas.writer_options({ALPHA: "feed", BRAVO: "feed"})
    assert [(o["kind"], o["current"]) for o in opts[ALPHA]] == [("tesla", False), ("teslafi", True)]
    assert [o["kind"] for o in opts[BRAVO]] == ["teslafi"], "nothing to choose between"


def test_a_kept_choice_is_the_cars_writer(two_teslas, monkeypatch):
    monkeypatch.setattr(two_teslas, "writers_chosen", lambda: {ALPHA: "tesla"})
    assert two_teslas.source_of({"vin": ALPHA}) == "tesla" and two_teslas.source_of({"vin": BRAVO}) == "feed"


def test_a_stream_alone_writes_its_car(two_teslas, monkeypatch):
    monkeypatch.setattr(two_teslas.teslafi, "vin_for_token", {})
    assert two_teslas.source_of({"vin": ALPHA}) == "tesla"


def test_a_switch_is_asked_for_at_once_and_done_in_the_background(two_teslas, client):  # noqa: F811
    for w in (two_teslas.tesla_stream_worker, two_teslas.teslafi, two_teslas.teslamate):
        w.last_rebuild[ALPHA] = T0
    r = client.post("/settings/sources/writer", data=form(two_teslas, {"vin": ALPHA, "kind": "tesla"}))
    assert r.status_code == 303 and r.headers["location"].split("?")[0].endswith("/settings/cars")
    assert not two_teslas.switched and len(two_teslas.jobs) == 1, "the request only records it"
    assert two_teslas.switcher.status(ALPHA)["state"] == "switching"
    assert two_teslas.choose_writer(ALPHA, "tesla") == "A switch for this car is already under way."
    two_teslas.jobs.pop()()
    assert two_teslas.switched == [(ALPHA, "tesla", "teslafi")]
    job = two_teslas.switcher.status(ALPHA)
    assert job["state"] == "done" and job["boundary"] == T0
    assert all(ALPHA not in w.last_rebuild for w in (two_teslas.tesla_stream_worker, two_teslas.teslafi, two_teslas.teslamate)), "every worker replays at its next tick"
    r = client.post("/settings/sources/writer", data=form(two_teslas, {"vin": ALPHA, "kind": "tesla", "back": "tesla"}))
    assert r.headers["location"].replace("?vin=" + ALPHA, "").endswith("/settings/sources/tesla#step-history")


def test_switching_away_from_the_stream_stops_its_live_positions_at_once(two_teslas, monkeypatch):
    monkeypatch.setattr(two_teslas, "writers_chosen", lambda: {ALPHA: "tesla"})
    monkeypatch.setattr(two_teslas.tesla_stream_worker, "writing", {ALPHA})
    assert two_teslas.choose_writer(ALPHA, "teslafi") is None
    two_teslas.jobs.pop()()
    assert two_teslas.switched == [(ALPHA, "teslafi", "tesla")] and ALPHA not in two_teslas.tesla_stream_worker.writing


def test_a_source_that_does_not_follow_the_car_is_refused(two_teslas, client):  # noqa: F811
    assert two_teslas.choose_writer(BRAVO, "tesla") == "That source does not follow this car."
    assert two_teslas.choose_writer("NOPE", "tesla") == "No such car."
    assert not two_teslas.jobs


def test_a_refused_switch_is_said_on_the_card(two_teslas, monkeypatch):
    monkeypatch.setattr(history_writer, "switch", lambda *a: (None, "The stream went silent from Oct 5 14:40 to 14:55 UTC."))
    assert two_teslas.choose_writer(ALPHA, "tesla") is None
    two_teslas.jobs.pop()()
    job = two_teslas.switcher.status(ALPHA)
    assert job["state"] == "refused" and "14:40" in job["problem"]
    html = cars_card(two_teslas, {ALPHA: "feed", BRAVO: "feed"})
    assert "Not switched to Tesla (direct): The stream went silent" in html


def test_a_stream_writer_gone_silent_is_a_problem_on_the_card(two_teslas, monkeypatch):
    monkeypatch.setattr(two_teslas, "writers_chosen", lambda: {ALPHA: "tesla"})
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(two_teslas.tesla_stream_worker, "state", {ALPHA: {"at": now - timedelta(minutes=45), "connected": True}})
    html = cars_card(two_teslas, {ALPHA: "tesla", BRAVO: "feed"})
    assert "The stream has been silent since" in html and "TeslaFi (API) still has its raw records from then: switch back to fill the gap." in html
    monkeypatch.setattr(two_teslas.tesla_stream_worker, "state", {ALPHA: {"at": now - timedelta(minutes=45), "connected": False}})
    assert "has been silent" not in cars_card(two_teslas, {ALPHA: "tesla", BRAVO: "feed"}), "a car that said goodbye is asleep"


def test_a_running_switch_shows_on_the_card(two_teslas):
    two_teslas.choose_writer(ALPHA, "tesla")
    assert "Switching to Tesla (direct)" in cars_card(two_teslas, {ALPHA: "feed", BRAVO: "feed"})


def kept(monkeypatch, site, vin, result=None, running=False, error=None):
    def get(v, days=7, start=True):
        return {"result": result, "error": error, "at": T0 if result else None, "running": running}

    monkeypatch.setattr(site.compare_worker, "get", get)


def a_result():
    day = T0.date()
    drive = {"start": T0, "end": T0 + timedelta(minutes=20), "miles": 10.0, "kwh": 2.5}
    return {
        "vin": ALPHA,
        "lo": T0 - timedelta(days=2),
        "hi": T0,
        "days": 2.0,
        "stream": {"drives": 2, "miles": 20.0, "kwh_used": 5.0, "charges": 1, "kwh_added": 30.0, "asleep_h": 30.5},
        "stored": {"drives": 1, "miles": 10.0, "kwh_used": 2.5, "charges": 1, "kwh_added": 29.0, "asleep_h": 31.0},
        "drives": [
            {"at": T0, "stream": drive, "stored": drive, "dt_s": -40, "off": False},
            {"at": T0 + timedelta(hours=3), "stream": drive, "stored": None, "off": True},
            {"at": T0 + timedelta(hours=4), "stream": {**drive, "n": 1}, "stored": {**drive, "n": 2}, "dt_s": 0, "off": True},
            {"at": T0 + timedelta(hours=5), "stream": drive, "stored": drive, "dt_s": 0, "off": False, "under_way": True},
        ],
        "charges": [{"at": T0, "stream": {"start": T0, "kwh": 30.0, "type": "AC"}, "stored": {"start": T0, "kwh": 29.0, "type": "AC"}, "dt_s": 30, "off": False}],
        "gaps": [{"day": day, "records": 81234, "resent": 120, "disconnects": 4, "reconnects": 5, "silences": 1, "longest_silence_min": 42.0}],
        "kwh_per_rated_mi": 0.25,
        "computed_at": T0,
    }


def test_the_comparison_page_shows_what_is_kept(two_teslas, client, monkeypatch):  # noqa: F811
    kept(monkeypatch, two_teslas, ALPHA, a_result())
    html = client.get(f"/settings/sources/tesla/compare?vin={ALPHA}&days=7").text
    assert "Stream beside history" in html and "TeslaFi (API)" in html
    assert "<td>2</td><td>1</td>" in html, "drives side by side"
    assert "missing" in html and "40 s apart" in html
    assert "1 vs 2" in html and "under way" in html, "a drive split differently, one still going at the window's end"
    assert "81,234" in html and "42.0 min" in html and "Resent" in html
    assert 'name="kind"' in html and "location.reload" not in html


def test_while_it_is_worked_out_the_page_says_so_and_reloads(two_teslas, client, monkeypatch):  # noqa: F811
    kept(monkeypatch, two_teslas, ALPHA, running=True)
    html = client.get(f"/settings/sources/tesla/compare?vin={ALPHA}").text
    assert "Working it out" in html and "location.reload" in html


def test_the_comparison_page_knows_only_known_cars(two_teslas, client):  # noqa: F811
    assert client.get("/settings/sources/tesla/compare?vin=NOPE").status_code == 404


def cars_card(site, writer):
    import jinja2
    from pathlib import Path

    import carspage
    import filters

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    choices = site.writer_options(writer)
    cards = carspage.cards([{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}], {}, writer, {}, {}, {}, [], choices=choices)
    for c in cards:
        c.update(site.writer_trouble(c["vin"], history_writer.KIND_OF_SOURCE.get(writer[c["vin"]]), c["choices"]))
    vehicles = [{"vin": ALPHA, "name": "Alpha"}, {"vin": BRAVO, "name": "Bravo"}]
    ctx = dict(root="/cc", now=T0, car={"vin": ALPHA, "name": "Alpha"}, active="", vehicles=vehicles, q="", colors={}, names={}, cur="$", st={}, conv={}, saved=False)
    return env.get_template("settings_cars.html").render(**ctx, section="cars", label="Cars", cars=cards, accounts=[], csrf="t")


def test_the_cars_card_offers_the_choice_only_where_there_is_one(two_teslas):
    html = cars_card(two_teslas, {ALPHA: "feed", BRAVO: "feed"})
    assert html.count('action="/cc/settings/sources/writer"') == 1 and f'value="{ALPHA}"' in html
    assert '<input type="radio" name="kind" value="teslafi" checked data-current>TeslaFi (API)' in html and '<input type="radio" name="kind" value="tesla">Tesla (direct)' in html
    assert f'href="/cc/settings/sources/tesla/compare?vin={ALPHA}">Compare</a>' in html and html.count('class="btn sm switch"') == 1, "Switch is there, shown by CSS once another source is picked"


def test_connect_tesla_step_6_shows_the_writer_the_choice_and_the_week():
    import jinja2
    from pathlib import Path

    import filters
    import teslapage

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")))
    filters.register(env)
    choices = history_writer.options("teslafi", {"tesla": True, "teslafi": True})
    step = [
        {"vin": ALPHA, "name": "Alpha", "choices": choices, "compare": {"result": a_result(), "error": None, "at": T0, "running": False}},
        {"vin": BRAVO, "name": "Bravo", "choices": history_writer.options("tesla", {"tesla": True}), "compare": {"result": None, "error": None, "at": None, "running": True}},
    ]
    ctx = dict(root="/cc", now=T0, car={"vin": ALPHA, "name": "Alpha"}, active="", vehicles=[], q="", colors={}, names={}, cur="$", st={}, conv={}, saved=False, label="Connect Tesla")
    html = env.get_template("settings_tesla.html").render(**ctx, steps=teslapage.steps({}), facts={}, links={}, stream=[], tesla_scopes=[], history_step=step, csrf="t")
    assert 'id="step-history"' in html and "History from" in html
    assert "Last 2 days · " in html and "2 differences<" in html
    assert html.count('name="back" value="tesla"') == 1, "a choice only where there is one"
    assert "Comparing the last week" in html and "Tesla (direct) writes it · " in html
    assert f"/settings/sources/tesla/compare?vin={BRAVO}" in html

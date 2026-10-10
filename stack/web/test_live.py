"""The live Now page: cards as partials, what changed, and the stream that sends it."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import jinja2
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import filters  # noqa: E402

T0 = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
CAR = {
    "vin": "V1",
    "name": "Alpha",
    "status": "Charging",
    "status_class": "charging",
    "charging_power_kw": 7.2,
    "soc": 58.0,
    "range_mi": 225.0,
    "charge_target": 90.0,
    "place": "Home",
    "live": True,
    "updated_age": "just now",
    "lat": 40.71,
    "lon": -74.0,
}


@pytest.fixture
def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
    filters.register(e)
    return e


def now_ctx(**kw):
    return dict(
        root="/cc",
        now=T0,
        car=CAR,
        active="now",
        vehicles=[{"vin": "V1", "name": "Alpha"}],
        q="",
        colors={},
        names={},
        cur="$",
        ribbon=[],
        spark="",
        software=[],
        due=[],
        spark_first=None,
        drives=[],
        charges=[],
        thumbs={},
        tread_last=None,
        ticks=[],
        today={"drives": 1, "miles": 3.1, "kwh": 17.4},
        **kw,
    )


def test_the_now_card_calls_the_guard_mode_by_the_cars_make(env):
    for make, word, other in (("Lucid", "Halo Secure", "Sentry"), ("Tesla", "Sentry", "Halo Secure")):
        ctx = {**now_ctx(), "car": {**CAR, "live": True, "sentry": True, "sentry_threat": True, "make": make}}
        hero = env.get_template("_now_hero.html").render(**ctx)
        assert f"{word} reacting to something" in hero and f"<dt>{word}</dt>" in hero and other not in hero


def test_the_now_card_is_a_partial_the_page_includes(env):
    ctx = now_ctx()
    page = env.get_template("now.html").render(**ctx)
    hero = env.get_template("_now_hero.html").render(**ctx)
    assert 'data-live="V1"' in hero and hero.strip() in page, "a streamed card is the same markup as a loaded one"


def test_each_garage_card_names_its_car(env):
    cars = [dict(CAR, ribbon=[], last_drive=None), dict(CAR, vin="V2", name="Bravo", ribbon=[], last_drive=None)]
    html = env.get_template("garage.html").render(
        root="/cc", now=T0, car={"vin": "all", "name": "Garage", "garage": True}, active="now", vehicles=[], q="", colors={}, names={}, cur="$", cars=cars, today={}, line=[], ticks=[]
    )
    assert 'data-live="V1"' in html and 'data-live="V2"' in html


def test_a_car_driving_a_route_says_where_to_and_how_long(env):
    route = {"name": "Bravo Market", "minutes": 8, "miles": 3.2}
    hero = env.get_template("_now_hero.html").render(**now_ctx(route=route))
    assert "to <b>Bravo Market</b>, 8 min" in hero
    assert "Bravo Market" not in env.get_template("_now_hero.html").render(**now_ctx(route=None)), "no route, no line"
    cars = [dict(CAR, ribbon=[], last_drive=None, route=dict(route, minutes=None)), dict(CAR, vin="V2", name="Bravo", ribbon=[], last_drive=None)]
    html = env.get_template("garage.html").render(
        root="/cc", now=T0, car={"vin": "all", "name": "Garage", "garage": True}, active="now", vehicles=[], q="", colors={}, names={}, cur="$", cars=cars, today={}, line=[], ticks=[]
    )
    assert html.count("to Bravo Market") == 1 and "to Bravo Market," not in html, "the garage card too, without minutes it does not know"


# ---- what changed, and how it is written on the wire
import json  # noqa: E402

import livestream  # noqa: E402


def card(html, lat=40.7, lon=-74.0):
    return {"html": html, "lat": lat, "lon": lon}


def test_the_first_look_sends_every_card():
    sent, prev = livestream.changed({}, {"V1": card("<a>1</a>"), "V2": card("<a>2</a>")})
    assert [s["vin"] for s in sent] == ["V1", "V2"] and prev == {"V1": "<a>1</a>", "V2": "<a>2</a>"}


def test_an_unchanged_card_is_not_sent_again():
    sent, prev = livestream.changed({"V1": "<a>1</a>"}, {"V1": card("<a>1</a>")})
    assert sent == [] and prev == {"V1": "<a>1</a>"}


def test_only_the_card_that_changed_is_sent():
    sent, prev = livestream.changed({"V1": "<a>1</a>", "V2": "<a>2</a>"}, {"V1": card("<a>1</a>"), "V2": card("<a>2!</a>", 41.0, -73.0)})
    assert sent == [{"vin": "V2", "html": "<a>2!</a>", "lat": 41.0, "lon": -73.0}] and prev["V2"] == "<a>2!</a>"


def test_a_card_that_is_gone_is_forgotten_and_not_sent():
    sent, prev = livestream.changed({"V1": "<a>1</a>", "V2": "<a>2</a>"}, {"V1": card("<a>1</a>")})
    assert sent == [] and prev == {"V1": "<a>1</a>"}


def test_a_frame_is_one_event_and_one_data_line_of_json():
    frame = livestream.sse("card", {"vin": "V1", "html": "<section>\n  <b>58%</b>\n</section>"})
    lines = frame.split("\n")
    assert lines[0] == "event: card" and lines[1].startswith("data: ") and frame.endswith("\n\n") and len(lines) == 4
    assert json.loads(lines[1][6:])["html"].count("\n") == 2, "newlines travel inside the JSON, never as raw lines"
    assert livestream.HEARTBEAT.startswith(":") and livestream.HEARTBEAT.endswith("\n\n")


# ---- the stream itself
from test_empty import bare, client  # noqa: E402,F401 - fixtures


@pytest.fixture
def bounded(bare, monkeypatch):  # noqa: F811
    """One look, then the stream ends: a test cannot wait on a stream that never does."""
    monkeypatch.setattr(bare, "STREAM_MAX_S", 0)
    monkeypatch.setattr(bare, "STREAM_S", 0.01)
    return bare


def frames(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.split("\n") if line.startswith("data: ")]


def test_the_stream_is_an_event_stream_that_proxies_leave_alone(bounded, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bounded, "live_cards", lambda vin, root: {"V1": {"html": '<section data-live="V1">58%</section>', "lat": 40.7, "lon": -74.0}})
    r = client.get("/stream?vin=V1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert frames(r.text) == [{"vin": "V1", "html": '<section data-live="V1">58%</section>', "lat": 40.7, "lon": -74.0}]


def test_a_card_that_cannot_be_drawn_sends_nothing_and_does_not_end_the_stream(bounded, client, monkeypatch):  # noqa: F811
    def broken(vin, root):
        raise RuntimeError("connector down")

    monkeypatch.setattr(bounded, "live_cards", broken)
    r = client.get("/stream?vin=V1")
    assert r.status_code == 200 and frames(r.text) == []


def test_the_stream_asks_for_the_car_and_the_mount_it_was_opened_on(bounded, client, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(bounded, "live_cards", lambda vin, root: seen.append((vin, root)) or {})
    client.get("/stream?vin=all")
    client.get("/stream")
    assert seen == [("all", ""), (None, "")]


def test_one_cars_card_is_the_now_partial(bare, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(CAR))
    monkeypatch.setattr(bare, "today_by_car", lambda: {"V1": {"drives": 2, "miles": 9.0}})
    monkeypatch.setattr(bare, "service_due", lambda vin, odo: [{"what": "Tire rotation", "status": "overdue"}])
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    cards = bare.live_cards(None, "/carcommand")
    assert list(cards) == ["V1", "activity"] and cards["V1"]["lat"] == 40.71
    assert cards["activity"]["html"] == '<div class="acts" data-live="activity" data-sig=""></div>', "no car out: the banners' place is empty"
    html = cards["V1"]["html"]
    assert 'data-live="V1"' in html and "2 drives today" in html and 'href="/carcommand/service?vin=V1"' in html, "links keep the mount and the car"


def test_the_garage_streams_one_card_per_car(bare, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(CAR, vin=vin, name={"V1": "Alpha", "V2": "Bravo"}[vin]))
    monkeypatch.setattr(bare, "ribbon", lambda vin, hours=24, end=None: [])
    monkeypatch.setattr(bare, "drives", lambda vin, n=None, *a: [])
    monkeypatch.setattr(bare, "today_by_car", lambda: {})
    cards = bare.live_cards("all", "")
    assert set(cards) == {"V1", "V2", "activity"} and 'data-live="V2"' in cards["V2"]["html"] and "Bravo" in cards["V2"]["html"]


def test_the_stream_sends_the_banners_with_the_cards(bare, monkeypatch):  # noqa: F811
    from datetime import timedelta

    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}, {"vin": "V2", "name": "Bravo"}])
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(CAR, vin=vin, name={"V1": "Alpha", "V2": "Bravo"}[vin]))
    monkeypatch.setattr(bare, "ribbon", lambda vin, hours=24, end=None: [])
    monkeypatch.setattr(bare, "drives", lambda vin, n=None, *a: [])
    monkeypatch.setattr(bare, "today_by_car", lambda: {})
    seen = []
    banner = {"vin": "V2", "name": "Bravo", "kind": "charging", "place": "Home", "kw": 7.2, "level_from": 40.0, "level_now": 58.0, "since": timedelta(minutes=50), "kwh_added": None}
    monkeypatch.setattr(bare, "activity_banners", lambda cars: seen.append([c["vin"] for c in cars]) or [banner])
    html = bare.live_cards("all", "/cc")["activity"]["html"]
    assert seen == [["V1", "V2"]], "every car the garage draws is asked about"
    assert 'data-sig="V2:charging"' in html and 'href="/cc/?vin=V2"' in html and "<b>Bravo</b> charging at <b>Home</b>" in html
    assert "7.2 kW · 40→58% · 50 min" in html


def test_a_page_that_names_a_stream_says_so_on_main(env):
    html = env.get_template("now.html").render(**now_ctx(), live_vin="V1")
    assert '<main data-live-src="/cc/stream?vin=V1">' in html
    plain = env.get_template("now.html").render(**now_ctx())
    assert "<main>" in plain and "data-live-src" not in plain


def test_the_browser_side_swaps_cards_and_rests_while_hidden():
    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    assert "main[data-live-src]" in js and "EventSource" in js and "visibilitychange" in js and '"cc:live"' in js


def test_the_garage_strip_is_drawn_to_the_minute_so_a_quiet_card_is_not_resent(bare, monkeypatch):  # noqa: F811
    """Its hour ticks sit at a percentage of the last 24 hours; drawn to the second, every
    card differed on every look and all of them were sent every 15 s."""
    ends = []
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}])
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(CAR))
    monkeypatch.setattr(bare, "ribbon", lambda vin, hours=24, end=None: [])
    monkeypatch.setattr(bare, "drives", lambda vin, n=None, *a: [])
    monkeypatch.setattr(bare, "today_by_car", lambda: {})
    monkeypatch.setattr(bare.nowpage, "strip_ticks", lambda start, end, tz: ends.append(end) or [])
    bare.live_cards("all", "")
    assert ends and ends[0].second == 0 and ends[0].microsecond == 0


def test_the_garage_strip_is_read_once_a_minute_and_drawn_to_that_minute(bare, monkeypatch):  # noqa: F811
    """Read on its own clock it changed at a different second than the ticks, and every card
    went out twice a minute."""
    calls = []
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}])
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(CAR))
    monkeypatch.setattr(bare, "ribbon", lambda vin, hours=24, end=None: calls.append(end) or [])
    monkeypatch.setattr(bare, "drives", lambda vin, n=None, *a: [])
    monkeypatch.setattr(bare, "today_by_car", lambda: {})
    bare.LIVE_RIBBONS.clear()
    bare.draw_live_cards("all", "")
    bare.draw_live_cards("all", "")  # drawn twice, past the ten-second sharing
    assert len(calls) == 1 and calls[0].second == 0, "one read per car per minute, drawn to the minute the ticks use"


# ---- the review's fixes
def test_a_live_page_does_not_also_reload_itself(env):
    live = env.get_template("now.html").render(**now_ctx(), live_vin="V1")
    assert 'http-equiv="refresh"' not in live and "Refreshes every minute" not in live
    assert "<footer" not in live or "just now" not in live.split("<footer")[1], "the footer's age would go stale on a page that is not reloaded; the card carries it"
    still = env.get_template("now.html").render(**now_ctx())
    assert 'http-equiv="refresh"' in still, "a page without a stream keeps its minute reload"


def test_the_streamed_card_gets_everything_the_page_gives_it(bare, monkeypatch):  # noqa: F811
    """The tread line vanished on the first event: the page passed it, the stream did not.
    Both now take the card's data from one place."""
    monkeypatch.setattr(bare, "live_vehicle", lambda vin=None: dict(CAR))
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha"}])
    monkeypatch.setattr(bare, "now_card_data", lambda car: {"today": None, "due": [], "tread_last": {"measured_on": T0, "front_left": 8.0, "front_right": 8.0, "rear_left": 7.0, "rear_right": 7.0}})
    bare._cache.clear()
    html = bare.live_cards("V1", "")["V1"]["html"]
    assert "tread" in html.lower()


def test_open_tabs_share_one_drawing_of_the_cards(bare, monkeypatch):  # noqa: F811
    drawn = []
    monkeypatch.setattr(bare, "draw_live_cards", lambda vin, root: drawn.append(vin) or {"V1": {"html": "x", "lat": None, "lon": None}})
    bare._cache.clear()
    for _ in range(5):
        bare.live_cards("V1", "")
    assert drawn == ["V1"], "five tabs within ten seconds cost one drawing"


def test_the_stream_answers_before_the_first_drawing(bounded, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bounded, "live_cards", lambda vin, root: {})
    body = client.get("/stream?vin=V1").text
    assert body.startswith(": keep"), "headers and a first line go out at once, before a slow connector is asked"


def test_a_stream_that_cannot_draw_says_so_once_not_every_look(bounded, client, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(bounded, "STREAM_MAX_S", 0.2)
    monkeypatch.setattr(bounded, "STREAM_S", 0.01)

    def broken(vin, root):
        raise RuntimeError("connector down")

    monkeypatch.setattr(bounded, "live_cards", broken)
    with caplog.at_level("WARNING"):
        client.get("/stream?vin=V1")
    assert sum("connector down" in r.message for r in caplog.records) == 1


def test_a_page_whose_stream_is_refused_reloads_once_to_sign_in_again():
    js = (Path(__file__).parent / "static" / "shell.js").read_text()
    assert "EventSource.CLOSED" in js and "sessionStorage" in js

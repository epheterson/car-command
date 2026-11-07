"""The Powerwall on the house's own network: the proxy's readings as rows on the app's scale, a
minute's samples as one row, and the cloud handed the live reading when the relay goes quiet."""

from datetime import datetime, timedelta, timezone

import energy
import energy_relay as er

NOW = datetime(2026, 7, 14, 19, 0, 5, tzinfo=timezone.utc)
AGG = {"site": {"instant_power": -15}, "battery": {"instant_power": 1050.0}, "load": {"instant_power": 1037.75}, "solar": {"instant_power": -10}}


def test_the_charge_is_put_on_the_app_scale():
    assert er.soe_app(51.9) == 49.4 and er.soe_app(100) == 100.0 and er.soe_app(5) == 0.0
    assert er.soe_app(2.0) == 0.0 and er.soe_app(None) is None and er.soe_app(True) is None and er.soe_app("51") is None


def test_a_reading_keeps_the_powerwall_signs_and_reads_night_draw_as_no_sun():
    r = er.reading_from(AGG, {"percentage": 51.9})
    assert r == {"solar_w": 0.0, "home_w": 1037.75, "grid_w": -15.0, "battery_w": 1050.0, "soe_pct": 49.4}
    assert er.reading_from({"load": {"instant_power": 900}}, {}) is None, "no source said anything"
    assert er.reading_from({"solar": {"instant_power": 2000}, "site": {"instant_power": 100}}, {})["home_w"] == energy.home_from(2000.0, None, 100.0)


def test_a_minute_is_the_mean_of_its_samples_and_its_last_charge():
    m = NOW.replace(second=0)
    row = er.minute_row(m, [{"solar_w": 100, "home_w": 300, "grid_w": 200, "battery_w": 0, "soe_pct": 50.0}, {"solar_w": 300, "home_w": 500, "grid_w": None, "battery_w": 200, "soe_pct": None}])
    assert row == {"at": m, "solar_w": 200.0, "home_w": 400.0, "grid_w": 200.0, "battery_w": 100.0, "soe_pct": 50.0}


def worker(clock):
    live, written = energy.Live(), []
    w = er.RelayWorker(url=lambda: "http://relay.test:8675", live=live, write=lambda rows: written.extend(rows), now=lambda: clock[0])
    return w, live, written


def answering(path):
    return AGG if path == "/aggregates" else {"percentage": 51.9}


def down(path):
    raise OSError("unreachable")


def test_the_worker_keeps_the_live_reading_and_writes_each_finished_minute():
    clock = [NOW]
    w, live, written = worker(clock)
    for s in (0, 15, 30, 45):
        clock[0] = NOW + timedelta(seconds=s)
        w.step(answering)
    assert live.get(clock[0])["source"] == "relay" and written == [], "the minute is not over"
    clock[0] = NOW + timedelta(seconds=60)
    w.step(answering)
    assert len(written) == 1 and written[0]["at"] == NOW.replace(second=0) and written[0]["home_w"] == 1037.8
    assert w.fresh(clock[0]) and w.status["ok"]


def test_a_quiet_relay_hands_the_live_reading_back_to_the_cloud():
    clock = [NOW]
    w, live, written = worker(clock)
    w.step(answering)
    clock[0] = NOW + timedelta(seconds=er.FRESH_S - 1)
    w.step(down)
    assert w.fresh(clock[0]) and w.status == {"ok": False, "at": clock[0], "error": "OSError"}
    clock[0] = NOW + timedelta(seconds=er.FRESH_S)
    assert not w.fresh(clock[0]), "the cloud's live poll takes over"
    assert not er.RelayWorker(url=lambda: "", live=live, write=written.extend).fresh(NOW), "no relay set: never fresh"


def test_no_relay_set_asks_nothing_and_an_address_set_later_is_read_at_the_next_pass(monkeypatch):
    w, live, _ = worker([NOW])
    asked, urls = [], iter(["", "http://relay.springfield.test:8675", "http://relay.springfield.test:8675"])

    def url():
        u = next(urls, None)
        if u is None:
            w.stop()
            return "http://relay.springfield.test:8675"
        return u

    monkeypatch.setattr(er, "http_get", lambda base: asked.append(base) or answering)
    monkeypatch.setattr(w._stop_event, "wait", lambda timeout: w._stop_event.is_set())
    w.url = url
    w.run()
    assert asked == ["http://relay.springfield.test:8675"], "one fetcher, made when the address appeared"
    assert w.status["ok"] and live.get(NOW)["source"] == "relay"


def test_an_address_is_a_base_url_or_nothing():
    assert er.address("192.168.1.20:8675") == "http://192.168.1.20:8675"
    assert er.address(" https://relay.springfield.test/ ") == "https://relay.springfield.test"
    for bad in ("ftp://relay", "http://", "http://user@relay", "http://relay?x=1", "http://relay:99999", "javascript:alert(1)"):
        assert er.address(bad) is None, bad

"""Connect Tesla reads kept answers: the page never waits on Tesla, and asking in the background
costs no more Tesla calls than opening the page did."""

import json
import threading
import time

import pytest

import teslapage
import tesla_keys
from test_empty import bare, client  # noqa: F401 - fixtures
from test_tesla_routes import ALPHA, BRAVO, HOST, streaming, tesla  # noqa: F401 - fixtures


class Tesla:
    """A fake Fleet API that answers every call by its path and counts them."""

    def __init__(self, pem):
        self.pem, self.sent = pem, []

    def __call__(self, method, url, *, headers, data=None, json=None):
        self.sent.append((method, url))
        if url.endswith("/oauth2/v3/token"):
            return 200, {"access_token": "partner"}
        if "/partner_accounts/public_key" in url:
            return 200, {"response": {"public_key": tesla_keys.public_hex(self.pem)}}
        if "/fleet_telemetry_errors" in url:
            return 200, {"response": {"fleet_telemetry_errors": []}}
        if url.endswith("/fleet_telemetry_config"):
            return 200, {"response": {"synced": True, "config": {"hostname": HOST, "port": 8443}}}
        if url.endswith("/fleet_status"):
            return 200, {"response": {"key_paired_vins": [ALPHA, BRAVO], "vehicle_info": {}}}
        if url.endswith("/api/1/vehicles"):
            return 200, {"response": [{"vin": ALPHA, "display_name": "Alpha"}, {"vin": BRAVO, "display_name": "Bravo"}]}
        raise AssertionError(url)


class Woken:
    def __init__(self):
        self.woken, self.rounds = 0, 0

    def wake(self):
        self.woken += 1


@pytest.fixture
def kept(streaming, monkeypatch):  # noqa: F811
    """A full setup (key registered, account, two cars, one streaming) with nothing kept yet."""
    import tesla_fleet

    app = streaming.app
    streaming.rows[0]["config"] = {"base": tesla_fleet.NA, "stream": [ALPHA]}
    streaming.rows[0]["plain"] = json.dumps({"access_token": "old", "refresh_token": "r1", "expires_at": time.time() + 10 * 3600})
    monkeypatch.setattr(app, "TESLA_DOMAIN", "example.test")
    monkeypatch.setattr(app, "tesla_public_check", lambda pem, host=None: None)
    monkeypatch.setattr(app.tesla_net, "reach", lambda host: {"ok": True, "problem": None, "error": None, "expires": None})
    monkeypatch.setattr(app, "stream_heard", lambda vins: {})
    monkeypatch.setattr(app, "all_vehicles", lambda: [])
    monkeypatch.setattr(app, "tesla_billing", lambda: None)
    monkeypatch.setattr(app, "tesla_history_step", lambda facts: [])
    fake = Tesla(streaming.pem)
    monkeypatch.setattr(app, "tesla_app", lambda: app.tesla_fleet.Fleet("client-alpha", "ta-secret.springfield", call=fake))
    woken = Woken()
    monkeypatch.setattr(app, "tesla_refresher", woken)
    app.TESLA_CACHE.clear()
    streaming.fake, streaming.woken = fake, woken
    yield streaming
    app.TESLA_CACHE.clear()


def test_with_nothing_kept_the_page_says_it_is_checking_and_calls_nobody(kept, client):  # noqa: F811
    r = client.get("/settings/sources/tesla")
    assert r.status_code == 200 and "Checking with Tesla…" in r.text and 'id="setup"' not in r.text
    assert kept.fake.sent == [] and kept.woken.woken == 1, "the refresher is woken; the page does not wait for it"


def test_the_page_renders_from_what_the_refresher_kept_without_calling_tesla(kept, client):  # noqa: F811
    kept.app.tesla_facts()  # the refresher's round
    asked = len(kept.fake.sent)
    assert asked == 10, "partner token, the key twice (site and stream, a token each), cars, status, one config, errors (and its token)"
    r = client.get("/settings/sources/tesla")
    assert r.status_code == 200 and 'id="setup"' in r.text and "Streaming now" not in r.text and "Checking with Tesla" not in r.text
    assert 'class="step done" id="step-keys"' in r.text
    assert len(kept.fake.sent) == asked, "the page called nobody"


def test_answers_past_their_time_still_render_and_wake_the_refresher(kept, client, monkeypatch):  # noqa: F811
    kept.app.tesla_facts()
    asked, now = len(kept.fake.sent), time.time()
    monkeypatch.setattr(kept.app.time, "time", lambda: now + 20)
    r = client.get("/settings/sources/tesla")
    assert 'id="setup"' in r.text and "Checking with Tesla" not in r.text and kept.woken.woken == 1, "fleet status (8 s) is past its time"
    monkeypatch.setattr(kept.app.time, "time", lambda: now + 3600)
    assert "Checking with Tesla…" in client.get("/settings/sources/tesla").text, "an hour old: shown, and said to be checking"
    assert len(kept.fake.sent) == asked


def test_after_an_action_cleared_what_was_kept_the_page_asks_tesla_as_before(kept, client):  # noqa: F811
    kept.woken.rounds = 1
    r = client.get("/settings/sources/tesla")
    assert 'id="setup"' in r.text and "Checking with Tesla" not in r.text and len(kept.fake.sent) == 10


def test_a_refusal_is_kept_for_the_page_and_asked_again_next_round(kept, client):  # noqa: F811
    import tesla_fleet

    kept.app.TESLA_CACHE["vehicles"] = (time.time(), tesla_fleet.TeslaError("Tesla is down."))
    kept.app.tesla_facts()
    assert any(u.endswith("/api/1/vehicles") for _, u in kept.fake.sent), "a kept refusal is not an answer"
    kept.app.TESLA_CACHE["vehicles"] = (time.time(), tesla_fleet.TeslaError("Tesla is down."))
    facts, _ = kept.app.tesla_held()
    assert facts["account_error"] == "Tesla is down."


def test_tesla_calls_an_hour_are_no_more_than_opening_the_page_cost(kept, monkeypatch):
    """The budget. Idle: one round a start, nothing after. Someone on the page, a load every
    10 s for an hour: a round after each load that found something past its time, never more
    than the page asked when it called Tesla itself."""
    app = kept.app
    clock = [time.time()]
    monkeypatch.setattr(app.time, "time", lambda: clock[0])

    def hour(view):
        app.TESLA_CACHE.clear()
        kept.fake.sent.clear()
        start = clock[0]
        for t in range(0, 3600, 10):
            clock[0] = start + t
            view()
        clock[0] = start + 3600
        return len(kept.fake.sent)

    before = hour(lambda: app.tesla_facts())  # the page asking Tesla on each load

    def now_():
        app.tesla_held()
        if kept.woken.woken:
            kept.woken.woken = 0
            app.tesla_facts()  # the refresher's round

    after = hour(now_)
    assert after <= before, (after, before)
    assert (before, after) == (564, 564), "each answer keeps its own ttl, so the rounds ask exactly what the loads did"


def test_the_refresher_asks_once_at_start_and_then_only_when_woken():
    rounds = []
    r = teslapage.Refresher(lambda: rounds.append(1), warm_s=0)
    r.start()
    try:
        deadline = time.time() + 2
        while not rounds and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        assert len(rounds) == 1 and r.rounds == 1, "the warm round, then nothing until a page wakes it"
        r.wake()
        deadline = time.time() + 2
        while len(rounds) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(rounds) == 2
    finally:
        r.stop()
        r.join(2)
    assert not r.is_alive()


def test_a_round_that_fails_leaves_the_refresher_running():
    calls = threading.Event()

    def boom():
        calls.set()
        raise RuntimeError("database away")

    r = teslapage.Refresher(boom, warm_s=0)
    r.start()
    try:
        assert calls.wait(2)
        calls.clear()
        r.wake()
        assert calls.wait(2)
    finally:
        r.stop()
        r.join(2)
    assert not r.is_alive()

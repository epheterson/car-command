"""The Connect Tesla checklist: which step is done, which is next, and what blocks it."""

from datetime import datetime, timedelta, timezone

import pytest

import teslapage

ALPHA, BRAVO = "5YJ3E1EA0JF000001", "5YJ3E1EA0JF000002"


def facts(**kw):
    base = {
        "app_ok": False,
        "app_error": None,
        "key": False,
        "public_ok": False,
        "public_error": None,
        "registered": False,
        "register_error": None,
        "account": False,
        "account_error": None,
        "cars": [],
    }
    return {**base, **kw}


def states(f):
    return [(s["id"], s["state"]) for s in teslapage.steps(f)]


def test_with_nothing_set_the_app_keys_come_first():
    assert states(facts()) == [("app", "next"), ("domain", "waiting"), ("signin", "waiting"), ("keys", "waiting"), ("stream", "waiting")]


def test_an_app_that_tesla_refuses_is_a_problem_with_its_reason():
    s = teslapage.steps(facts(app_error="Tesla refused (401): invalid_client"))
    assert s[0]["state"] == "problem" and "invalid_client" in s[0]["detail"]
    assert [x["state"] for x in s[1:]] == ["waiting"] * 4


def test_working_keys_lead_to_the_domain():
    assert states(facts(app_ok=True)) == [("app", "done"), ("domain", "next"), ("signin", "waiting"), ("keys", "waiting"), ("stream", "waiting")]


def test_a_key_tesla_cannot_read_from_outside_is_the_domain_problem():
    s = teslapage.steps(facts(app_ok=True, key=True, public_error="garage answered 302 (a login page?)"))
    assert s[1]["state"] == "problem" and "302" in s[1]["detail"]


def test_a_registered_domain_leads_to_sign_in():
    assert states(facts(app_ok=True, key=True, public_ok=True, registered=True))[1:3] == [("domain", "done"), ("signin", "next")]


def test_an_account_that_needs_a_new_sign_in_says_so():
    s = teslapage.steps(facts(app_ok=True, key=True, public_ok=True, registered=True, account=True, account_error="Tesla asked to sign in again."))
    assert s[2]["state"] == "problem" and s[3]["state"] == "waiting"


def test_keys_are_next_until_every_car_holds_one():
    cars = [{"vin": ALPHA, "name": "Alpha", "paired": True}, {"vin": BRAVO, "name": "Bravo", "paired": False}]
    s = teslapage.steps(facts(app_ok=True, key=True, public_ok=True, registered=True, account=True, cars=cars))
    assert s[3]["state"] == "next" and "1 of 2" in s[3]["detail"]
    cars[1]["paired"] = True
    assert teslapage.steps(facts(app_ok=True, key=True, public_ok=True, registered=True, account=True, cars=cars))[3]["state"] == "done"


def test_an_account_with_no_cars_is_a_problem_not_a_tick():
    s = teslapage.steps(facts(app_ok=True, key=True, public_ok=True, registered=True, account=True, cars=[]))
    assert s[3]["state"] == "problem"


def test_a_later_step_never_shows_done_while_an_earlier_one_is_not():
    s = teslapage.steps(facts(app_ok=False, registered=True, account=True, cars=[{"vin": ALPHA, "name": "Alpha", "paired": True}]))
    assert [x["state"] for x in s] == ["next", "waiting", "waiting", "waiting", "waiting"]


# ---- step 5: streaming
HOST = "carcommand-stream.tail0000.ts.net"
RECEIVER_OK = {"ok": True, "problem": None, "error": None, "expires": None}


def ready(**kw):
    """Steps 1 to 4 done: two cars, both holding the key."""
    cars = [{"vin": ALPHA, "name": "Alpha", "paired": True}, {"vin": BRAVO, "name": "Bravo", "paired": True}]
    base = facts(app_ok=True, key=True, public_ok=True, registered=True, account=True, cars=cars, stream_host=HOST, stream_host_ok=True, stream_registered=True, receiver=RECEIVER_OK)
    return {**base, **kw}


def stream_step(f):
    return teslapage.steps(f)[4]


def on(vin, synced=True, **kw):
    return {vin: {"on": True, "config_known": True, "config": {"synced": synced, "limit_reached": False}, **kw}}


def test_without_a_stream_address_the_page_says_how_to_make_one():
    s = stream_step(ready(stream_host=None, stream_host_ok=False))
    assert s["state"] == "next" and "TESLA_STREAM_HOST" in s["detail"] and "docs/tesla-stream.md" in s["detail"]


def test_an_address_with_a_scheme_or_port_is_named_as_the_problem():
    s = stream_step(ready(stream_host="https://x.test:8443", stream_host_ok=False))
    assert s["state"] == "problem" and "https://x.test:8443" in s["detail"]


def test_an_address_with_tesla_in_it_is_refused_before_tesla_refuses_it():
    s = stream_step(ready(stream_host="carcommand-tesla.tail0000.ts.net"))
    assert s["state"] == "problem" and "refuses a domain" in s["detail"] and "carcommand-stream" in s["detail"]


def test_the_funnel_nodes_default_name_is_one_tesla_accepts():
    from pathlib import Path

    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    names = [line.split("=", 1)[1].strip() for line in compose.splitlines() if "TS_HOSTNAME=" in line]
    assert names == ["carcommand-stream"] and "hostname: carcommand-stream" in compose


def test_an_address_tesla_does_not_know_yet_asks_to_register_it():
    s = stream_step(ready(stream_registered=False))
    assert s["state"] == "next" and "register" in s["detail"].lower()


def test_a_key_the_stream_address_does_not_serve_is_the_problem():
    s = stream_step(ready(stream_registered=False, stream_public_error=f"{HOST} answered 404 for the key."))
    assert s["state"] == "problem" and "404" in s["detail"]


@pytest.mark.parametrize(
    "problem,fix",
    [("unreachable", "make tesla-up"), ("certificate", "HTTPS certificates"), ("expiring", "make tesla-restart")],
)
def test_each_receiver_failure_has_a_sentence_and_a_fix(problem, fix):
    s = stream_step(ready(receiver={"ok": problem == "expiring", "problem": problem, "error": "The receiver said so.", "expires": None}))
    assert s["state"] == "problem" and s["detail"].startswith("The receiver said so.") and fix in s["detail"]


def test_with_no_car_started_the_step_asks_to_start_one():
    s = stream_step(ready())
    assert s["state"] == "next" and "Start" in s["detail"]


def test_a_started_car_that_has_not_taken_it_yet_waits_for_its_wake():
    s = stream_step(ready(stream_cars=on(ALPHA, synced=False)))
    assert s["state"] == "next" and "Alpha" in s["detail"] and "wakes" in s["detail"]


def test_a_synced_car_is_done():
    assert stream_step(ready(stream_cars=on(ALPHA)))["state"] == "done"


def test_a_config_tesla_wiped_says_billing_limit_and_start_again():
    s = stream_step(ready(stream_cars={ALPHA: {"on": True, "config_known": True, "config": None}}))
    assert s["state"] == "problem" and "billing limit" in s["detail"] and "Start again" in s["detail"]


def test_a_car_streaming_to_five_apps_says_so():
    s = stream_step(ready(stream_cars=on(ALPHA, config={"synced": False, "limit_reached": True})))
    assert s["state"] == "problem" and "five apps" in s["detail"]


def test_a_streaming_car_that_lost_its_key_sends_you_back_to_step_four():
    f = ready(stream_cars=on(ALPHA))
    f["cars"][0]["paired"] = False
    s = teslapage.steps(f)
    assert s[3]["state"] == "problem" and "Alpha no longer holds the app's key" in s[3]["detail"] and s[4]["state"] == "waiting"
    assert "no longer holds" in teslapage.stream_cars(f)[0]["said"]


@pytest.mark.parametrize("reason,words", [("missing_key", "step 4"), ("unsupported_firmware", "2024.26"), ("max_configs", "five apps"), ("something_new", "something new")])
def test_every_reason_tesla_skips_a_car_reads_as_a_sentence(reason, words):
    assert words in teslapage.skipped_text(reason, "Alpha") and "Alpha" in teslapage.skipped_text(reason, "Alpha")


def test_the_key_link_names_the_domain_and_the_car():
    assert teslapage.key_link("example.test", ALPHA) == f"https://tesla.com/_ak/example.test?vin={ALPHA}"


# ---- a car that streams, and Tesla's errors -------------------------------------------------
def test_a_config_sent_again_while_the_car_streams_is_not_a_wait():
    """A new field list re-sends every started car's config; Tesla says not synced until the
    car's next wake, but a car heard from is streaming on the config it holds."""
    heard = datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)
    f = ready(stream_cars=on(ALPHA, synced=False, last_at=heard))
    assert stream_step(f)["state"] == "done"
    assert teslapage.stream_cars(f)[0]["said"] is None
    never = ready(stream_cars=on(ALPHA, synced=False, last_at=None))
    assert "wakes" in stream_step(never)["detail"], "a car never heard from still waits"


NOW = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
TIMEOUT = "webconnection error: dial tcp 203.0.113.7:8443: i/o timeout cm_type=stream app=evotlb42"


def test_tesla_errors_are_one_line_per_kind_from_the_last_day_without_the_app_id():
    errs = [{"name": "evotlb42", "body": TIMEOUT, "at": (NOW - timedelta(hours=h)).isoformat().replace("+00:00", "Z")} for h in (1, 3, 7)]
    errs += [{"name": "evotlb42", "body": TIMEOUT, "at": (NOW - timedelta(hours=30)).isoformat()}]
    errs += [{"name": "evotlb42", "body": "x509: certificate signed by unknown authority", "at": (NOW - timedelta(hours=2)).isoformat()}]
    got = teslapage.error_summary(errs, "Alpha", NOW)
    assert [(e["kind"], e["n"]) for e in got] == [("unreachable", 3), ("certificate", 1)], "deduped by kind, newest first, the one a day old gone"
    assert got[0]["what"] == "Tesla couldn't reach the receiver" and got[0]["last"] == NOW - timedelta(hours=1)
    assert all("evotlb42" not in e["raw"] for e in got) and "i/o timeout" in got[0]["raw"]


def test_tesla_not_answering_this_page_is_current_and_anything_else_is_named():
    got = teslapage.error_summary([{"name": "Tesla", "body": "Tesla took too long", "at": None}, {"name": "", "body": "something odd", "at": NOW.isoformat()}], "Alpha", NOW)
    assert {e["kind"] for e in got} == {"asked", "other"} and any(e["what"] == "Alpha reported an error" for e in got)
    assert teslapage.error_summary([], "Alpha", NOW) == []

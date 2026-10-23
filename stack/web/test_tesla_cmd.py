"""Tesla commands through the signing proxy: what is sent, waking a sleeping car, and every
answer as one plain sentence. A fake proxy answers from a queue; nothing leaves the test."""

import httpx
import pytest

import commands as cm
import tesla_cmd as tc

VIN = "5YJ3E1EA0JF000001"


class FakeProxy:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, method, url, *, headers, json=None):
        self.sent.append((method, url, headers.get("Authorization"), json))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


OK = (200, {"response": {"result": True, "reason": ""}})


def proxy(*answers):
    fake, t = FakeProxy(*answers), [0.0]

    def sleep(s):
        t[0] += s

    return tc.Proxy("https://localhost:4443/", call=fake, sleep=sleep, clock=lambda: t[0]), fake


def test_every_tesla_command_in_the_catalogue_has_an_endpoint():
    for key, c in cm.CATALOGUE.items():
        if key != "wake" and (c.makes is None or "Tesla" in c.makes):
            assert key in tc.ENDPOINTS, key


def test_a_command_posts_its_body_with_the_token_in_the_header_only():
    p, fake = proxy(OK)
    assert p.send("tok", VIN, "charge_limit", 80) == tc.Result(True)
    method, url, auth, body = fake.sent[0]
    assert (method, url, auth, body) == ("POST", f"https://localhost:4443/api/1/vehicles/{VIN}/command/set_charge_limit", "Bearer tok", {"percent": 80})
    assert "tok" not in url


def test_a_sleeping_car_is_woken_then_the_command_sent_again():
    p, fake = proxy(
        (408, {"error": "vehicle unavailable: vehicle is offline or asleep"}),
        (200, {"response": {"state": "asleep"}}),
        (200, {"response": {"state": "online"}}),
        OK,
    )
    r = p.send("tok", VIN, "charge_stop")
    assert r.ok and r.woke
    assert [s[1].rsplit("/", 1)[-1] for s in fake.sent] == ["charge_stop", "wake_up", VIN, "charge_stop"]


def test_a_car_that_will_not_wake_is_said_and_the_wait_is_bounded():
    asleep = (200, {"response": {"state": "asleep"}})
    p, fake = proxy((408, {"error": "asleep"}), asleep, *[asleep] * 40)
    r = p.send("tok", VIN, "charge_start")
    assert not r.ok and "didn't wake" in r.detail
    assert len(fake.sent) <= 2 + tc.WAKE_S // tc.WAKE_POLL_S + 1


def test_wake_alone():
    p, _ = proxy((200, {"response": {"state": "online"}}))
    assert p.send("tok", VIN, "wake").ok


@pytest.mark.parametrize(
    "answer, says",
    [
        ((401, {"error": "token expired"}), "new sign-in"),
        ((403, {"error": "Unauthorized missing scopes"}), "Sign in again to allow commands"),
        ((500, {"error": "vehicle rejected request: your public key has not been paired with the vehicle"}), "key"),
        ((429, {"error": "rate limit"}), "limiting"),
        ((503, None), "servers"),
        ((200, {"response": {"result": False, "reason": "disconnected"}}), "cable"),
        ((200, {"response": {"result": False, "reason": "something_new"}}), "The car said no (something_new)."),
        ((200, "<html>"), "unreadable"),
    ],
)
def test_refusals_are_short_sentences(answer, says):
    p, _ = proxy(answer)
    r = p.send("tok", VIN, "charge_start")
    assert not r.ok and says in r.detail
    assert "tok" not in r.detail


def test_already_in_that_state_is_a_success():
    p, _ = proxy((200, {"response": {"result": False, "reason": "not_charging"}}))
    r = p.send("tok", VIN, "charge_stop")
    assert r.ok and r.detail == "It already was."


def test_a_proxy_that_does_not_answer_is_unavailable():
    p, _ = proxy(httpx.ConnectError("refused"))
    with pytest.raises(tc.Unavailable, match="proxy"):
        p.send("tok", VIN, "lock")


def test_health():
    p, _ = proxy((200, None))
    assert p.health()
    p, _ = proxy(httpx.ConnectError("refused"))
    assert not p.health()

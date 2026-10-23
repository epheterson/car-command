"""Who and where, in words, for the sign-in alerts."""

import pytest

import devices
from test_guard import app_module, client  # noqa: F401 - fixtures

MAC_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
IPHONE_SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


@pytest.mark.parametrize(
    "agent, said",
    [
        (MAC_CHROME, "Chrome on a Mac"),
        (IPHONE_SAFARI, "Safari on an iPhone"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0", "Firefox on Windows"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0", "Edge on Windows"),
        ("Mozilla/5.0 (Linux; Android 14; Pixel) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36", "Chrome on an Android phone"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 CriOS/120.0 Mobile/15E148 Safari/604.1", "Chrome on an iPhone"),
        ("curl/8.4.0", "curl"),
        ("python-requests/2.31", "a script"),
        ("", ""),
        (None, ""),
        ("something nobody has heard of", ""),
    ],
)
def test_a_user_agent_becomes_a_browser_and_a_machine(agent, said):
    assert devices.browser(agent) == said


def test_the_house_and_the_docker_gateway_are_your_network_a_public_address_is_itself():
    for inside in ("172.18.0.1", "10.0.0.5", "192.168.1.20", "127.0.0.1", "100.101.102.103", "fd12::5", "172.18.0.1 via 172.18.0.1"):
        assert devices.where(inside) == "your network", inside
    assert devices.where("203.0.113.9") == "203.0.113.9"
    assert devices.where("203.0.113.9 via 172.18.0.1") == "203.0.113.9", "the proxy between is not where anyone sits"
    assert devices.where("?") == "" and devices.where(None) == ""


def test_an_alert_logged_before_this_reads_in_words_too():
    old = f"Signed in to Car Command from 172.18.0.1 at Oct 22, 3:00 PM · {MAC_CHROME}"
    assert devices.humane(old) == "Signed in to Car Command from your network at Oct 22, 3:00 PM · Chrome on a Mac"
    assert devices.humane("Signed in to Car Command from 203.0.113.9 via 172.18.0.1 at Oct 22, 3:00 PM · Mozilla/5.0 (X11; Linux x86_64) Firefox/121.0") == (
        "Signed in to Car Command from 203.0.113.9 at Oct 22, 3:00 PM · Firefox on Linux"
    )
    assert devices.humane("Signed in to Car Command from 172.18.0.1 at Oct 22, 3:00 PM · Mozilla/5.0 (mystery)") == "Signed in to Car Command from your network at Oct 22, 3:00 PM"
    for plain in ("Alpha finished charging at Home: 80%", "Garage door opened at 7:41 AM", ""):
        assert devices.humane(plain) == plain


def test_the_sign_in_alert_names_the_browser_and_the_kind_of_place(app_module, client, monkeypatch):  # noqa: F811
    import test_guard

    seen = []
    monkeypatch.setattr(app_module.recorder, "notify", lambda text, kind="other", vin=None: seen.append((kind, text)))
    token = test_guard._token_from(client.get("/login").text)
    r = client.post("/login", data={"password": "a good long password", "csrf_token": token}, headers={"user-agent": MAC_CHROME})
    assert r.status_code == 303
    ((kind, text),) = seen
    assert kind == "login" and "Mozilla" not in text and "AppleWebKit" not in text and "Chrome on a Mac" in text
    assert "172." not in text

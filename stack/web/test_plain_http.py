"""Plain http: Settings says so, and a tier 3 command from off the home network is refused."""

from types import SimpleNamespace

from test_command_routes import TESLA, csrf, site  # noqa: F401 - fixtures
from test_empty import bare, client  # noqa: F401 - fixtures (a client off the home network)


def req(bare, scheme="http", peer="203.0.113.9", headers=None):  # noqa: F811
    h = {k.lower(): v for k, v in (headers or {}).items()}
    return SimpleNamespace(headers=h, url=SimpleNamespace(scheme=scheme), scope={"cc_peer": peer}, client=None)


def test_plain_http_is_read_from_the_browsers_side(bare):  # noqa: F811
    assert bare.plain_http(req(bare))
    assert not bare.plain_http(req(bare, "https"))
    assert not bare.plain_http(req(bare, "http", headers={"X-Forwarded-Proto": "https"})), "a proxy that ended https"
    assert bare.plain_http(req(bare, "https", headers={"X-Forwarded-Proto": "http"}))


def test_tier_three_over_plain_http_goes_only_from_home(bare):  # noqa: F811
    assert bare.tier3_refused_here(req(bare), "unlock") == bare.PLAIN_HTTP_AWAY
    assert bare.tier3_refused_here(req(bare, peer="192.168.1.20"), "unlock") is None, "a phone at home"
    assert bare.tier3_refused_here(req(bare, "https"), "unlock") is None, "https from anywhere"
    assert bare.tier3_refused_here(req(bare), "lock") is None, "only the commands that open the car"
    assert bare.tier3_refused_here(req(bare), "nope") is None
    away = req(bare, peer="192.168.1.2", headers={"X-Forwarded-For": "203.0.113.9"})
    assert bare.tier3_refused_here(away, "frunk") == bare.PLAIN_HTTP_AWAY, "a proxy at home passing on a stranger"


def test_the_access_page_says_not_encrypted_on_plain_http(client):  # noqa: F811
    page = client.get("/settings/access").text
    assert 'id="plain-http"' in page and "Not encrypted" in page and "install.md#https" in page


def test_a_tap_from_away_over_plain_http_is_refused_before_anything_else(site, client):  # noqa: F811
    r = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=csrf(site["app"]))
    assert r.status_code == 403 and r.json()["said"] == site["app"].PLAIN_HTTP_AWAY and site["log"] == []

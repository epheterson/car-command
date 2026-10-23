"""The selected car stays selected: every in-site page link a Settings page renders carries ?vin=,
including All cars, so the sidebar never switches car on a click."""

import re

import pytest

from carlinks import keep_car_links, wants_car
from settingsform import SECTIONS
from test_empty import bare, client  # noqa: F401 - fixtures

LINK = re.compile(r'<a\b[^>]*?\shref="([^"]*)"')


def test_a_page_link_gains_the_car_and_nothing_else_does():
    html = (
        '<a href="/settings/sources/tesla">a</a><a href="/settings/cars#import">b</a><a class="x" href="/settings/maps?saved=1">c</a>'
        '<a href="/settings/cars?vin=V2">d</a><a href="/drives/12">e</a><a href="/static/x.css">f</a><a href="https://example.com/settings">g</a><a href="/">h</a><a href="/stats">i</a>'
    )
    out = keep_car_links(html, "", "all")
    assert LINK.findall(out) == [
        "/settings/sources/tesla?vin=all",
        "/settings/cars?vin=all#import",
        "/settings/maps?saved=1&amp;vin=all",
        "/settings/cars?vin=V2",
        "/drives/12",
        "/static/x.css",
        "https://example.com/settings",
        "/?vin=all",
        "/stats?vin=all",
    ]
    assert keep_car_links('<a href="/cc/settings">a</a><a href="/settings">b</a>', "/cc", "V1") == '<a href="/cc/settings?vin=V1">a</a><a href="/settings">b</a>'
    assert keep_car_links(html, "", "") == html
    assert wants_car("/settings/") and not wants_car("/settings-old") and not wants_car("/api/now")


@pytest.mark.parametrize("vin", ["all", "V2"])
def test_every_page_link_of_every_settings_page_carries_the_selection(bare, client, monkeypatch, vin):  # noqa: F811
    monkeypatch.setattr(bare, "all_vehicles", lambda: [{"vin": "V1", "name": "Alpha", "live": True}, {"vin": "V2", "name": "Bravo", "live": True}])
    monkeypatch.setattr(bare, "live_vehicle", lambda v=None: dict(bare.empty_car(), vin=v or "V1", name="Bravo"))
    seen = 0
    for key, _ in [("", "")] + list(SECTIONS) + [("sources/tesla", ""), ("money/plan", "")]:
        path = "/settings/" + key if key else "/settings"
        r = client.get(f"{path}?vin={vin}")
        assert r.status_code == 200, path
        for href in LINK.findall(r.text):
            if href.startswith("/") and wants_car(href.split("?")[0].split("#")[0]):
                seen += 1
                assert "vin=" in href, f"{path} links to {href} without the car"
                if f"vin={vin}" not in href:  # the car switcher: the same page, another car
                    assert href.split("?")[0] == path, f"{path} links to {href}, another car"
    assert seen > 40


def test_a_redirect_to_a_page_keeps_the_car_and_other_redirects_do_not_change():
    from carlinks import redirect_with_car as keep

    assert keep("/settings/access?saved=1", "", "all") == "/settings/access?saved=1&vin=all"
    assert keep("/cc/settings/sources/tesla#pw", "/cc", "V1") == "/cc/settings/sources/tesla?vin=V1#pw"
    assert keep("/places", "", "V1") == "/places?vin=V1"
    assert keep("/settings/cars?vin=V2", "", "V1") == "/settings/cars?vin=V2"
    assert keep("/", "", "V1") == "/" and keep("/login?next=%2F", "", "V1") == "/login?next=%2F"
    assert keep("https://example.com/settings", "", "V1") == "https://example.com/settings"
    assert keep("/settings", "", None) == "/settings"


def test_a_form_posted_from_a_car_page_comes_back_to_it(bare, client):  # noqa: F811
    import auth

    token = auth.csrf_token(bare.AUTH["secret"], None)
    r = client.post("/settings/tokens/abc/revoke", data={"csrf_token": token}, headers={"referer": "http://testserver/settings/access?vin=all", "host": "testserver"})
    assert r.status_code == 303 and r.headers["location"].endswith("vin=all"), r.headers.get("location")

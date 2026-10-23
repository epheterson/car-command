from datetime import datetime, timezone

import garagedoor

T0 = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)
AWAY = {"a": {"at_home": False, "lat": 1.0, "lon": 2.0}, "b": {"at_home": False, "lat": 3.0, "lon": 4.0}}
HOME = {"a": {"at_home": True, "lat": 1.0, "lon": 2.0}, "b": {"at_home": False, "lat": 3.0, "lon": 4.0}}


def entity(state, since=T0):
    return {"entity_id": "cover.garage_door", "state": state, "last_changed": since.isoformat().replace("+00:00", "Z")}


def door(state, since=T0):
    return garagedoor.door_from(entity(state, since))


def test_the_door_reads_open_closed_and_moving_and_nothing_else():
    assert door("closed") == {"state": "closed", "open": False, "since": T0}
    assert door("opening")["open"] and door("closing")["open"] and door("open")["open"]
    assert door("unavailable") is None and door("unknown") is None and garagedoor.door_from(None) is None


def test_a_car_home_with_an_unknown_place_elsewhere_still_counts_as_home():
    assert garagedoor.car_home(HOME) is True
    assert garagedoor.car_home(AWAY) is False
    assert garagedoor.car_home({"c": {"at_home": False, "lat": None, "lon": None}, **AWAY}) is None
    assert garagedoor.car_home({}) is None


def test_a_status_token_without_places_sees_the_door_but_not_whether_a_car_is_home():
    d = door("open")
    assert garagedoor.event(d, False, share_place=False) == {"state": "open", "open": True, "since": T0.isoformat()}
    assert garagedoor.event(d, False, share_place=True)["car_home"] is False
    assert garagedoor.event(None, False, share_place=True) is None


def test_the_reader_asks_home_assistant_once_per_tick_and_survives_it_failing():
    calls, clock = [], [0.0]

    def get():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("no route")
        return entity("open")

    r = garagedoor.Reader(get=get, clock=lambda: clock[0])
    assert r.now(AWAY, True)["open"] is True
    r.now(AWAY, True)
    assert len(calls) == 1
    clock[0] += garagedoor.READ_S
    assert r.now(AWAY, True) is None
    clock[0] += garagedoor.READ_S
    assert r.now(AWAY, True)["car_home"] is False


class Answer:
    def __init__(self, code, body=None):
        self.status_code, self.body = code, body or {}

    def json(self):
        return self.body


def test_an_address_is_taken_as_a_base_and_anything_else_is_refused():
    assert garagedoor.ha_url("homeassistant.local:8123/") == "http://homeassistant.local:8123"
    assert garagedoor.ha_url(" https://ha.springfield.test ") == "https://ha.springfield.test"
    for bad in ("", "ftp://ha", "http://user:pw@ha", "http://ha/?x=1"):
        try:
            garagedoor.ha_url(bad)
            raise AssertionError(bad)
        except garagedoor.LinkError:
            pass


def test_the_sign_in_link_names_this_site_as_the_client():
    url = garagedoor.authorize_url("http://ha:8123", "https://cc.test/", "https://cc.test/settings/alerts/ha/callback", "s1")
    assert url.startswith("http://ha:8123/auth/authorize?") and "client_id=https%3A%2F%2Fcc.test%2F" in url and "response_type=code" in url and "state=s1" in url


def test_garage_doors_are_offered_first():
    states = [
        {"entity_id": "light.porch", "attributes": {}},
        {"entity_id": "cover.blinds", "attributes": {"device_class": "blind", "friendly_name": "Blinds"}},
        {"entity_id": "cover.garage_door", "attributes": {"device_class": "garage", "friendly_name": "Garage"}},
    ]
    assert garagedoor.doors(states) == [{"id": "cover.garage_door", "name": "Garage"}, {"id": "cover.blinds", "name": "Blinds"}]


def test_the_access_token_is_kept_until_a_minute_before_it_runs_out_and_a_refusal_is_said():
    sent, t = [], [1000.0]

    def post(url, data, timeout):
        sent.append((url, data))
        return Answer(200, {"access_token": f"a{len(sent)}", "expires_in": 1800}) if len(sent) < 3 else Answer(400)

    link = garagedoor.Link(post=post)
    link.use({"url": "http://ha:8123", "client": "https://cc.test/", "entity": "cover.g"}, "r1")
    assert link.ready() and link.bearer(lambda: t[0]) == "a1" and link.bearer(lambda: t[0] + 1700) == "a1"
    assert link.bearer(lambda: t[0] + 1750) == "a2"
    assert sent[0] == ("http://ha:8123/auth/token", {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "https://cc.test/"})
    try:
        link.bearer(lambda: t[0] + 9999)
        raise AssertionError("a refused refresh must say so")
    except garagedoor.LinkError as err:
        assert "Connect again" in str(err) and link.problem == str(err) and "r1" not in str(err)


def test_the_linked_home_assistant_wins_over_env(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://env:8123")
    monkeypatch.setenv("HA_TOKEN", "envtoken")
    link = garagedoor.Link(post=lambda url, data, timeout: Answer(200, {"access_token": "linked", "expires_in": 1800}))
    monkeypatch.setattr(garagedoor, "LINK", link)
    assert garagedoor.source() == ("http://env:8123", "envtoken", "cover.garage_door")
    link.use({"url": "http://ha:8123", "client": "https://cc.test/", "entity": "cover.g"}, "r1")
    assert garagedoor.source() == ("http://ha:8123", "linked", "cover.g")

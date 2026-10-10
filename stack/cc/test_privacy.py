"""Coordinates leave the box rounded to about 100 m, never at full precision."""

import pytest
import requests

from privacy import METHODS, install, rounded


class FakeOSM:
    def location_from_lat_lon(self, latitude, longitude, location=None):
        return (latitude, longitude, location)

    def gas_station_from_lat_lon(self, latitude, longitude, radius, location=None):
        return (latitude, longitude, radius)

    def charging_station_from_lat_lon(self, latitude, longitude, radius, location=None):
        return (latitude, longitude, radius)

    def amenity_from_lat_lon(self, latitude, longitude, radius, amenity, with_fallback=False):
        return (latitude, longitude, amenity)

    def _amenity_json_from_lat_lon(self, latitude, longitude, radius, amenity):
        return (latitude, longitude)


def test_rounded_trims_positional_and_keyword_coordinates():
    f = rounded(lambda self, latitude, longitude, *a, **k: (latitude, longitude))
    assert f(None, 39.801234567, -89.654321987) == (39.801, -89.654)
    assert f(None, latitude=39.801234567, longitude=-89.654321987) == (39.801, -89.654)
    assert f(None, None, None) == (None, None)


def test_install_wraps_every_method_that_sends_a_position():
    install(FakeOSM)
    osm = FakeOSM()
    assert osm.location_from_lat_lon(39.8012345, -89.6543219, None)[:2] == (39.801, -89.654)
    assert osm.gas_station_from_lat_lon(latitude=39.8016, longitude=-89.6544, radius=500)[:2] == (39.802, -89.654)
    assert set(METHODS) == {n for n in vars(FakeOSM) if n.endswith("_from_lat_lon")}


def test_install_twice_rounds_once():
    install(FakeOSM)
    install(FakeOSM)
    assert FakeOSM().location_from_lat_lon(39.80149, -89.65449)[:2] == (39.801, -89.654)


def test_install_without_carconnectivity_is_harmless():
    install(None)


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append((url, kw.get("timeout")))
        return "ok"


def make_fake_service():
    """A fresh class each test: install_redirect patches __init__ on whatever class it is
    given, and patches accumulate on a class reused across tests."""

    class FakeService:
        def __init__(self, *a, **k):
            self.osm_session = FakeSession()

    return FakeService


def test_local_geocoder_rewrites_the_public_host(monkeypatch):
    from privacy import install_redirect

    monkeypatch.setenv("CC_GEOCODER_URL", "http://maps:8090/")
    cls = make_fake_service()
    install_redirect(cls)
    s = cls()
    s.osm_session.get("https://nominatim.openstreetmap.org/reverse", params={})
    assert s.osm_session.inner.urls == [("http://maps:8090/reverse", 5)]


def test_off_means_no_request_at_all(monkeypatch):
    from privacy import install_redirect

    monkeypatch.setenv("CC_GEOCODER_URL", "")
    cls = make_fake_service()
    install_redirect(cls)
    s = cls()
    r = s.osm_session.get("https://nominatim.openstreetmap.org/reverse", params={})
    assert r.status_code == 503 and s.osm_session.inner.urls == []


def test_unset_leaves_the_session_alone(monkeypatch):
    from privacy import install_redirect

    monkeypatch.delenv("CC_GEOCODER_URL", raising=False)
    cls = make_fake_service()
    install_redirect(cls)
    assert isinstance(cls().osm_session, FakeSession)


class FlakySession:
    """Stands in for a real requests.Session talking to a maps container that is down or
    slow: .get() raises the way requests does, not the way a cooperative fake would."""

    def __init__(self, exc):
        self.exc = exc

    def get(self, url, **kw):
        raise self.exc


@pytest.mark.parametrize("exc", [requests.exceptions.ConnectionError("refused"), requests.exceptions.Timeout("no answer")])
def test_a_dead_local_maps_service_answers_503_not_an_exception(monkeypatch, exc):
    """OSMLocationService catches only requests.exceptions.RetryError, and its retry
    adapter is mounted on https:// only — our rewritten URL is http://. Left alone, a
    ConnectionError or Timeout from a down maps container would raise straight into the
    connector's drive/charge handling instead of just naming nothing."""
    from privacy import install_redirect

    monkeypatch.setenv("CC_GEOCODER_URL", "http://maps:8090/")
    cls = make_fake_service()
    install_redirect(cls)
    s = cls()
    s.osm_session.inner = FlakySession(exc)
    r = s.osm_session.get("https://nominatim.openstreetmap.org/reverse", params={})
    assert r.status_code == 503
    assert r.json() == {}
    with pytest.raises(requests.HTTPError):
        r.raise_for_status()

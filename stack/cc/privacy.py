"""Round every position CarConnectivity's OSM location service sends to Nominatim, and
route that lookup: local maps when CC_GEOCODER_URL says it is up, the public Nominatim
when unset (the site chose public), nowhere when it is empty (off, or no decision yet). Loaded into every
Python process in this image by sitecustomize.py.
"""

from __future__ import annotations

import functools
import logging
import os

import requests

PLACES = 3
METHODS = (
    "location_from_lat_lon",
    "gas_station_from_lat_lon",
    "charging_station_from_lat_lon",
    "amenity_from_lat_lon",
    "_amenity_json_from_lat_lon",
)
LOG = logging.getLogger("carcommand.privacy")


def _r(v):
    return round(v, PLACES) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def rounded(fn):
    """fn(self, latitude, longitude, ...) called with both coordinates rounded."""
    if getattr(fn, "_cc_rounded", False):
        return fn

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        args = tuple(_r(a) if i < 2 else a for i, a in enumerate(args))
        for k in ("latitude", "longitude"):
            if k in kwargs:
                kwargs[k] = _r(kwargs[k])
        return fn(self, *args, **kwargs)

    wrapper._cc_rounded = True
    return wrapper


def install(cls=None) -> None:
    """Wrap the OSM service's position-sending methods. Never raises: a connector that cannot
    be patched still runs, and the miss is logged."""
    try:
        if cls is None:
            from carconnectivity_services.location.osm_location_service import OSMLocationService as cls  # noqa: N813
        for name in METHODS:
            if hasattr(cls, name):
                setattr(cls, name, rounded(getattr(cls, name)))
    except Exception as err:  # noqa: BLE001 — must never stop the connector starting
        LOG.warning("could not round positions sent to the OSM location service: %s", err)


class _Off:
    """A response that says "no location", so OSMLocationService's status-code check fails
    cleanly instead of the connector crashing on an exception it never catches."""

    status_code = 503
    ok = False
    text = ""
    headers: dict = {}

    def json(self):
        return {}

    def raise_for_status(self):
        raise requests.HTTPError("503 maps unavailable")


class _Redirect:
    """A requests session stand-in: the public Nominatim host becomes the local one, or
    nothing. OSMLocationService only mounts its retry adapter on `https://`, our target is
    `http://`, and it catches only requests.exceptions.RetryError — a dead local maps
    service otherwise raises ConnectionError/Timeout straight into the connector. Every
    verb goes through one place (`_call`) so none of that reaches the caller."""

    def __init__(self, inner, base: str) -> None:
        self.inner, self.base = inner, base

    def _call(self, verb: str, url, **kw):
        if not self.base:
            return _Off()
        kw.setdefault("timeout", 5)
        try:
            return getattr(self.inner, verb)(url.replace("https://nominatim.openstreetmap.org/", self.base), **kw)
        except requests.RequestException as err:
            LOG.warning("local maps did not answer (%s): %s", verb, err)
            return _Off()

    def get(self, url, **kw):
        return self._call("get", url, **kw)

    def post(self, url, **kw):
        return self._call("post", url, **kw)

    def head(self, url, **kw):
        return self._call("head", url, **kw)

    def request(self, method, url, **kw):
        return self._call(method.lower(), url, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def install_redirect(cls=None) -> None:
    """When CC_GEOCODER_URL is set, give each new OSM service a session that rewrites the
    public host to it (the empty string means off: every request becomes a synthetic 503).
    Unset, this does nothing and the service keeps today's session. Never raises."""
    if "CC_GEOCODER_URL" not in os.environ:
        return
    base = os.environ["CC_GEOCODER_URL"]
    try:
        if cls is None:
            from carconnectivity_services.location.osm_location_service import OSMLocationService as cls  # noqa: N813
        orig = cls.__init__

        def __init__(self, *a, **k):
            orig(self, *a, **k)
            self.osm_session = _Redirect(self.osm_session, base)

        cls.__init__ = __init__
    except Exception as err:  # noqa: BLE001
        LOG.warning("could not route the OSM location service: %s", err)

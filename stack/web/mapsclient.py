"""The site's side of the local maps add-on. Every call has a short timeout and answers None
on any failure: the add-on is optional, and a page must never wait on it."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

MATCH_TIMEOUT_S = 60.0  # a long drive is several Valhalla calls; never on a page path
_REGION_ID = re.compile(r"[a-z0-9][a-z0-9/_-]*")


def available(status: dict | None) -> bool:
    return bool(status) and status.get("version") is not None


def valid_region_id(value: str) -> bool:
    """A Geofabrik id, lowercase and slash-separated, never a parent escape. Checked here and
    at the route: this is the only gate between a text box and a path on the add-on's own
    filesystem, so both the client and the route refuse before a call is ever made."""
    if not value or len(value) > 80 or not _REGION_ID.fullmatch(value):
        return False
    return ".." not in value.split("/")


_COORDS = re.compile(r"-?\d+\.\d+\s*,\s*-?\d+\.\d+")
_REASONS = {"where your cars drive": "Your drives", "added by hand": "Added by you", "refresh": "Refreshing"}


def region_name(region: dict) -> str:
    """The region's own name when it has one ("Northern California"); else its id's last
    segment in title case ("us/new-york" -> "New York"), never the raw path."""
    rid = region.get("id") or ""
    name = (region.get("name") or "").strip()
    if name and name != rid and not _REGION_ID.fullmatch(name):
        return name
    last = (rid or name).rsplit("/", 1)[-1]
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", last) if w) or rid


def region_reason(reason: str | None) -> str:
    """Why a region is here, in words. Older installs stored "a drive near <lat>, <lon>";
    a coordinate is never shown, so those read as the drives they came from."""
    text = (reason or "").strip()
    if not text:
        return ""
    if _COORDS.search(text):
        return "Your drives"
    return _REASONS.get(text, text[:1].upper() + text[1:])


class MapsClient:
    def __init__(self, base_url: str | None, timeout: float = 3.0) -> None:
        self.base = (base_url or "").rstrip("/") or None
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        if not self.base:
            return None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            return None

    def status(self):
        return self._call("GET", "/status")

    def ensure(self, lat: float, lon: float, reason: str):
        return self._call("POST", "/regions/ensure", {"lat": lat, "lon": lon, "reason": reason})

    def candidates(self, lat: float, lon: float, limit: int = 10):
        return self._call("GET", "/candidates?" + urllib.parse.urlencode({"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "limit": limit}))

    def search(self, q: str, lat: float | None = None, lon: float | None = None, limit: int = 8):
        """Typed text to addresses, streets and places in the add-on's own index, nearest
        to (lat, lon) first: {"results": [...], "built": bool}. The add-on answers from its
        local copy of the map; the text goes nowhere else."""
        params = {"q": (q or "")[:100], "limit": limit}
        if lat is not None and lon is not None:
            params.update(lat=f"{lat:.5f}", lon=f"{lon:.5f}")
        return self._call("GET", "/search?" + urllib.parse.urlencode(params))

    def match(self, points: list):
        """One drive's positions ([lat, lon, epoch s]) to the roads it took, matched on the
        add-on's own Valhalla: {"result": "matched", "points", "confidence", ...}, "no match"
        with a reason, or "busy"/"unavailable" (ask again later). Only the background route
        worker calls this, so it may take longer than a page ever would."""
        return self._call("POST", "/match", {"points": points}, timeout=MATCH_TIMEOUT_S)

    def reverse(self, lat: float, lon: float):
        """Nominatim's own shape (road/city under `address`), so maps_check can compare
        it against a stop's stored name the same way discover.py's live geocoding does."""
        return self._call("GET", "/reverse?" + urllib.parse.urlencode({"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"}))

    def add(self, region_id: str):
        if not valid_region_id(region_id):
            return None
        return self._call("POST", "/regions/add", {"id": region_id})

    def remove(self, region_id: str):
        if not valid_region_id(region_id):
            return None
        return self._call("DELETE", "/regions/" + urllib.parse.quote(region_id, safe="/"))

    def settings(self, **kw):
        return self._call("POST", "/settings", kw)

    def retry(self):
        return self._call("POST", "/retry")

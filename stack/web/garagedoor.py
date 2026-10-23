"""The garage door, read from Home Assistant, and whether it has been left open with no car home.

Home Assistant is linked from Settings › Alerts with its own sign-in (its OAuth flow: this site's
address is the client id, so nothing is registered anywhere), and the refresh token is sealed in
cc_state. HA_URL with a long-lived HA_TOKEN in .env is the fallback for an install without a browser.
Either can do anything in HA, so neither is ever logged, and only the one cover entity is read with
it. The read is kept for READ_S, so however many streams are open, HA is asked once per tick.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit

LOG = logging.getLogger("carcommand.garage")
READ_S = 15
OPEN_STATES = ("open", "opening", "closing")  # anything but closed leaves the garage open


STATE_KEY = "home_assistant"  # cc_state: {url, client, entity, sealed refresh token}
ENV_ENTITY = "cover.garage_door"


class LinkError(Exception):
    """Something Settings can say: what went wrong, never a token."""


def configured() -> bool:
    return LINK.ready() or bool(os.environ.get("HA_URL") and os.environ.get("HA_TOKEN"))


def ha_url(text: str) -> str:
    """The address the owner gave, as a base: scheme and host (and a path, if HA sits under one)."""
    text = (text or "").strip().rstrip("/")
    if "://" not in text and text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment or parts.username:
        raise LinkError("That is not a Home Assistant address: something like http://homeassistant.local:8123.")
    return text


def reach(ha: str) -> None:
    """Whether something that looks like Home Assistant answers at this address, before the browser is sent
    there: its API answers 401 to a caller with no token. LinkError says what is wrong."""
    try:
        code = _ping(f"{ha}/api/").status_code
    except Exception:  # noqa: BLE001 - refused, unknown host, timed out, bad certificate: all one answer for the person
        raise LinkError(f"Nothing answers at {ha}. Check the address, and that this machine can reach it.") from None
    if code not in (200, 401, 403):
        raise LinkError(f"{ha} answered {code}, which Home Assistant does not. Check the address and port.")


def authorize_url(ha: str, client: str, redirect: str, state: str) -> str:
    return f"{ha}/auth/authorize?" + urlencode({"client_id": client, "redirect_uri": redirect, "state": state, "response_type": "code"})


def token_call(post, ha: str, form: dict) -> dict:
    """POST /auth/token. HA answers 400 for a spent code or a revoked refresh token."""
    try:
        r = post(f"{ha}/auth/token", data=form, timeout=10)
    except Exception as err:  # noqa: BLE001 - the address is wrong or HA is down: say which, not the stack
        raise LinkError(f"Home Assistant did not answer at {ha} ({type(err).__name__}).") from None
    if r.status_code == 400:
        raise LinkError("Home Assistant refused the sign-in. Connect again.")
    if r.status_code >= 300:
        raise LinkError(f"Home Assistant answered {r.status_code}.")
    body = r.json()
    if not body.get("access_token"):
        raise LinkError("Home Assistant sent no token.")
    return body


def doors(states: list) -> list[dict]:
    """The covers an owner can pick, garage doors first: [{id, name}]."""
    covers = [s for s in states if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("cover.")]
    covers.sort(key=lambda s: ((s.get("attributes") or {}).get("device_class") != "garage", str(s.get("entity_id"))))
    return [{"id": s["entity_id"], "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]} for s in covers]


class Link:
    """The linked Home Assistant: its address, which door, and an access token kept fresh from the
    sealed refresh token. HA's access tokens last 30 minutes; one is fetched a minute before that."""

    def __init__(self, post=None) -> None:
        self.post = post
        self.lock = threading.Lock()
        self.url = self.client = self.entity = self.refresh = None
        self.access: str | None = None
        self.until = 0.0
        self.problem: str | None = None

    def use(self, kept: dict | None, refresh: str | None) -> None:
        with self.lock:
            kept = kept or {}
            self.url, self.client, self.entity, self.refresh = kept.get("url"), kept.get("client"), kept.get("entity"), refresh
            self.access, self.until, self.problem = None, 0.0, None

    def ready(self) -> bool:
        return bool(self.url and self.client and self.refresh)

    def bearer(self, clock=time.time) -> str:
        with self.lock:
            if not (self.url and self.client and self.refresh):
                raise LinkError("Home Assistant is not linked.")
            if self.access and clock() < self.until - 60:
                return self.access
            try:
                body = token_call(self.post or _post, self.url, {"grant_type": "refresh_token", "refresh_token": self.refresh, "client_id": self.client})
            except LinkError as err:
                self.problem = str(err)
                raise
            self.access, self.until, self.problem = body["access_token"], clock() + float(body.get("expires_in") or 1800), None
            return self.access


def save(conn, vault, ha: str, client: str, refresh: str, entity: str | None) -> dict:
    kept = {"url": ha, "client": client, "entity": entity, "sealed": vault.seal(refresh, "state", STATE_KEY).decode()}
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (STATE_KEY, json.dumps(kept)))
    return kept


def stored(conn) -> dict | None:
    row = conn.execute("SELECT value FROM cc_state WHERE key = %s", (STATE_KEY,)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def opened(conn, vault) -> tuple[dict | None, str | None]:
    kept = stored(conn)
    return kept, (vault.open(kept["sealed"].encode(), "state", STATE_KEY) if kept and kept.get("sealed") else None)


def forget(conn) -> None:
    conn.execute("DELETE FROM cc_state WHERE key = %s", (STATE_KEY,))


def door_from(entity: dict | None) -> dict | None:
    """The door from HA's entity state: {state, open, since}; None when HA does not know it."""
    if not isinstance(entity, dict):
        return None
    state = str(entity.get("state") or "").lower()
    if state not in OPEN_STATES + ("closed",):
        return None  # unavailable, unknown: say nothing rather than closed
    try:
        since = datetime.fromisoformat(str(entity.get("last_changed")).replace("Z", "+00:00"))
    except ValueError:
        since = None
    return {"state": state, "open": state in OPEN_STATES, "since": since}


def car_home(states: dict[str, dict]) -> bool | None:
    """Whether any car is home: True if one is, False if every car's place is known and none is,
    None when a car's place is unknown (then nothing can be said)."""
    cars = [s for s in states.values() if isinstance(s, dict)]
    if any(s.get("at_home") for s in cars):
        return True
    if not cars or any(s.get("lat") is None or s.get("lon") is None for s in cars):
        return None
    return False


def event(door: dict | None, home: bool | None, share_place: bool) -> dict | None:
    """The `garage` event as a stream sends it. car_home says where the cars are, so a status token
    sees it only when the owner lets that token see places; the door it always sees."""
    if door is None:
        return None
    out = {"state": door["state"], "open": door["open"], "since": door["since"].isoformat() if door["since"] else None}
    if share_place:
        out["car_home"] = home
    return out


class Reader:
    """The door, read at most every READ_S seconds, shared by every caller."""

    def __init__(self, get=None, clock=time.monotonic) -> None:
        self.get = get or _get
        self.clock = clock
        self.lock = threading.Lock()
        self.at = -1e9
        self.door: dict | None = None

    def read(self) -> dict | None:
        with self.lock:
            if self.clock() - self.at >= READ_S:
                self.at = self.clock()
                try:
                    self.door = door_from(self.get())
                except Exception as err:  # noqa: BLE001 - HA away is no door, never a broken stream
                    LOG.warning("garage door read failed: %s", type(err).__name__)
                    self.door = None
            return self.door

    def now(self, states: dict[str, dict], share_place: bool) -> dict | None:
        return event(self.read(), car_home(states), share_place)


def source() -> tuple[str, str, str] | None:
    """Where the door is read: the linked HA first, else .env's. (url, bearer, entity) or None."""
    if LINK.ready():
        return LINK.url, LINK.bearer(), LINK.entity or ENV_ENTITY
    if os.environ.get("HA_URL") and os.environ.get("HA_TOKEN"):
        return os.environ["HA_URL"].rstrip("/"), os.environ["HA_TOKEN"], os.environ.get("HA_GARAGE_ENTITY") or ENV_ENTITY
    return None


def open_cover(entity: str | None = None) -> None:
    """Open the door: HA's cover.open_cover on the chosen entity, nothing else."""
    _cover("open_cover", entity)


def close_cover(entity: str | None = None) -> None:
    """Close the door behind the car (garageclose): HA's cover.close_cover, nothing else."""
    _cover("close_cover", entity)


def _cover(service: str, entity: str | None) -> None:
    import httpx

    url, bearer, chosen = source() or (None, None, None)
    if not url:
        raise LinkError("Home Assistant is not linked.")
    r = httpx.post(f"{url}/api/services/cover/{service}", headers={"Authorization": f"Bearer {bearer}"}, json={"entity_id": entity or chosen}, timeout=10)
    r.raise_for_status()


def get_json(path: str, url: str | None = None, bearer: str | None = None):
    """GET one HA API path, with the linked HA (or .env's) unless an address and token are given."""
    import httpx

    if not url:
        url, bearer, _ = source() or (None, None, None)
    if not url:
        raise LinkError("Home Assistant is not linked.")
    r = httpx.get(f"{url}{path}", headers={"Authorization": f"Bearer {bearer}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def _get() -> dict | None:
    got = source()
    return get_json(f"/api/states/{got[2]}") if got else None


def _ping(url: str):
    import httpx

    return httpx.get(url, timeout=5, follow_redirects=False)


def _post(url: str, **kw):
    import httpx

    return httpx.post(url, **kw)


LINK = Link()
READER = Reader()

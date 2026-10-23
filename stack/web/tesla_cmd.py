"""Tesla commands through Tesla's own signing proxy (tesla-http-proxy, vehicle-command).

The proxy holds the app's private key and signs each command for the car; this module
speaks the Fleet API's REST shape to it, over TLS to a certificate made for it at start
and pinned here, with the account's OAuth access token in the header. The token never
appears in a URL, a log line or a message.

A car that is asleep answers 408: it is woken (wake_up, then its state asked every few
seconds, WAKE_S at most) and the command is sent once more. Every answer becomes one short
plain sentence; Tesla's own text is never shown as it came.

`call(method, url, *, headers, json) -> (status, body)` is httpx by default, so tests
answer from a queue and nothing here touches the network on its own.
"""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from urllib.parse import quote

import fleet_usage

WAKE_S = 45  # how long a sleeping car is given to come online
WAKE_POLL_S = 3

# command key -> (Fleet API command name, its JSON body from the argument)
ENDPOINTS = {
    "charge_start": ("charge_start", lambda a: {}),
    "charge_stop": ("charge_stop", lambda a: {}),
    "charge_limit": ("set_charge_limit", lambda a: {"percent": int(a)}),
    "charge_amps": ("set_charging_amps", lambda a: {"charging_amps": int(a)}),
    "climate_on": ("auto_conditioning_start", lambda a: {}),
    "climate_off": ("auto_conditioning_stop", lambda a: {}),
    "climate_temp": ("set_temps", lambda a: {"driver_temp": float(a), "passenger_temp": float(a)}),
    "defrost_on": ("set_preconditioning_max", lambda a: {"on": True}),
    "defrost_off": ("set_preconditioning_max", lambda a: {"on": False}),
    "charge_port_open": ("charge_port_door_open", lambda a: {}),
    "charge_port_close": ("charge_port_door_close", lambda a: {}),
    # 0 off, 1 keep, 2 dog, 3 camp
    "keep_on": ("set_climate_keeper_mode", lambda a: {"climate_keeper_mode": 1, "manual_override": True}),
    "dog_on": ("set_climate_keeper_mode", lambda a: {"climate_keeper_mode": 2, "manual_override": True}),
    "camp_on": ("set_climate_keeper_mode", lambda a: {"climate_keeper_mode": 3, "manual_override": True}),
    "keeper_off": ("set_climate_keeper_mode", lambda a: {"climate_keeper_mode": 0, "manual_override": True}),
    "overheat_on": ("set_cabin_overheat_protection", lambda a: {"on": True, "fan_only": False}),
    "overheat_fan": ("set_cabin_overheat_protection", lambda a: {"on": True, "fan_only": True}),
    "overheat_off": ("set_cabin_overheat_protection", lambda a: {"on": False, "fan_only": False}),
    "bioweapon_on": ("set_bioweapon_mode", lambda a: {"on": True, "manual_override": True}),
    "bioweapon_off": ("set_bioweapon_mode", lambda a: {"on": False, "manual_override": True}),
    # heater: 0 driver, 1 passenger, 2 rear left, 4 rear centre, 5 rear right
    "seat_heat": ("remote_seat_heater_request", lambda a: {"heater": 0, "level": int(a)}),
    "seat_heat_passenger": ("remote_seat_heater_request", lambda a: {"heater": 1, "level": int(a)}),
    "seat_heat_rear_left": ("remote_seat_heater_request", lambda a: {"heater": 2, "level": int(a)}),
    "seat_heat_rear_center": ("remote_seat_heater_request", lambda a: {"heater": 4, "level": int(a)}),
    "seat_heat_rear_right": ("remote_seat_heater_request", lambda a: {"heater": 5, "level": int(a)}),
    # seat_position: 1 driver, 2 passenger
    "seat_vent": ("remote_seat_cooler_request", lambda a: {"seat_position": 1, "seat_cooler_level": int(a)}),
    "seat_vent_passenger": ("remote_seat_cooler_request", lambda a: {"seat_position": 2, "seat_cooler_level": int(a)}),
    "wheel_heat_on": ("remote_steering_wheel_heater_request", lambda a: {"on": True}),
    "wheel_heat_off": ("remote_steering_wheel_heater_request", lambda a: {"on": False}),
    "media_toggle": ("media_toggle_playback", lambda a: {}),
    "media_next": ("media_next_track", lambda a: {}),
    "media_prev": ("media_prev_track", lambda a: {}),
    "volume": ("adjust_volume", lambda a: {"volume": float(a)}),
    "lock": ("door_lock", lambda a: {}),
    "unlock": ("door_unlock", lambda a: {}),
    "flash": ("flash_lights", lambda a: {}),
    "honk": ("honk_horn", lambda a: {}),
    "sentry_on": ("set_sentry_mode", lambda a: {"on": True}),
    "sentry_off": ("set_sentry_mode", lambda a: {"on": False}),
    "sched_charge": ("set_scheduled_charging", lambda a: {"enable": True, "time": int(a)}),
    "sched_charge_off": ("set_scheduled_charging", lambda a: {"enable": False, "time": 0}),
    "trunk": ("actuate_trunk", lambda a: {"which_trunk": "rear"}),
    "frunk": ("actuate_trunk", lambda a: {"which_trunk": "front"}),
    # The signed command takes no position; the REST shape still names one.
    "windows_vent": ("window_control", lambda a: {"command": "vent", "lat": 0, "lon": 0}),
    "windows_close": ("window_control", lambda a: {"command": "close", "lat": 0, "lon": 0}),
    "remote_start": ("remote_start_drive", lambda a: {}),
    # the argument is where the car is, (lat, lon): the car opens the door only near it
    "homelink": ("trigger_homelink", lambda a: {"lat": a[0], "lon": a[1]}),
}

# A refusal the car gives for a state that is already what was asked: the command did its job.
ALREADY = {"is_charging", "not_charging", "already_set", "already_on", "already_off"}
REASONS = {
    "disconnected": "The charge cable isn't plugged in.",
    "complete": "The battery is already at its limit.",
    "requested": "The car is already starting to charge.",
    "user_present": "Someone is in the car.",
    "cabin_overheat_protection_active": "Cabin overheat protection is running.",
    "charge_port_door_open": "The charge port door is open.",
}


@dataclass
class Result:
    ok: bool
    detail: str | None = None  # a short sentence beside the result, or why it failed
    woke: bool = False


class Unavailable(Exception):
    """The proxy is not set up or not answering: no command can go to a Tesla now."""


def _httpx(cert: str | None):
    def call(method, url, *, headers, json=None):
        import httpx

        # the proxy's own certificate, read on each call: it is made again when the proxy restarts
        verify = ssl.create_default_context(cafile=cert) if cert else True
        r = httpx.request(method, url, headers={"User-Agent": "carcommand/0.2", **headers}, json=json, timeout=60, verify=verify)
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, body

    return call


def _text(body) -> str:
    if isinstance(body, dict):
        return (
            " ".join(str(body.get(k) or "") for k in ("error", "error_description", "reason")) + " " + str((body.get("response") or {}).get("reason") if isinstance(body.get("response"), dict) else "")
        )
    return str(body or "")


def asleep(status: int, body) -> bool:
    text = _text(body).lower()
    return status == 408 or "asleep" in text or "offline" in text or "vehicle unavailable" in text


def problem(status: int, body) -> str:
    """One plain sentence for a refusal that is not a sleeping car."""
    text = _text(body).lower()
    if status == 401 or "token expired" in text or "invalid bearer" in text:
        return "Tesla needs a new sign-in. Connect Tesla › Sign in again."
    if "missing_scopes" in text or "missing scopes" in text or ("scope" in text and status == 403):
        return "Sign in again to allow commands."
    if "public key" in text or "not paired" in text or "unknown key" in text or "key not" in text:
        return "This car doesn't hold Car Command's key. Add it from Connect Tesla."
    if status == 429 or "rate limit" in text:
        return "Tesla is limiting commands. Try again in a few minutes."
    if status == 404:
        return "Tesla doesn't know this car on the signed-in account."
    if status >= 500:
        return "Tesla's servers didn't take the command. Try again shortly."
    return "Tesla refused the command."


class Proxy:
    """One account's commands through the proxy at `url` (https://localhost:4443 in the one
    container, https://tesla-proxy:4443 in compose), its certificate pinned from `cert`."""

    def __init__(self, url: str, cert: str | None = None, call=None, sleep=time.sleep, clock=time.monotonic):
        self.url, self.cert = url.rstrip("/"), cert
        self.call = call or _httpx(cert)
        self.sleep, self.clock = sleep, clock

    def _post(self, token: str, path: str, body: dict | None):
        import httpx

        try:
            url = f"{self.url}{path}"
            return fleet_usage.through("POST", url, body, lambda: self.call("POST", url, headers={"Authorization": f"Bearer {token}"}, json=body or {}))
        except fleet_usage.Overspent as err:
            raise Unavailable(err.public) from None
        except httpx.HTTPError as err:
            raise Unavailable(f"The Tesla signing proxy isn't answering ({type(err).__name__}).") from None

    def _get(self, token: str, path: str):
        import httpx

        try:
            url = f"{self.url}{path}"
            return fleet_usage.through("GET", url, None, lambda: self.call("GET", url, headers={"Authorization": f"Bearer {token}"}))
        except fleet_usage.Overspent as err:
            raise Unavailable(err.public) from None
        except httpx.HTTPError as err:
            raise Unavailable(f"The Tesla signing proxy isn't answering ({type(err).__name__}).") from None

    def wake(self, token: str, vin: str) -> bool:
        """Wake the car and wait until it says it is online, WAKE_S at most."""
        v = quote(vin, safe="")
        status, body = self._post(token, f"/api/1/vehicles/{v}/wake_up", None)
        if status >= 400 and not asleep(status, body):
            return False
        deadline = self.clock() + WAKE_S
        while True:
            state = ((body or {}).get("response") or {}).get("state") if isinstance(body, dict) else None
            if state == "online":
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(WAKE_POLL_S)
            status, body = self._get(token, f"/api/1/vehicles/{v}")

    def send(self, token: str, vin: str, key: str, arg=None) -> Result:
        if key == "wake":
            return Result(True, None, True) if self.wake(token, vin) else Result(False, "The car didn't wake up. Try again in a minute.")
        name, body = ENDPOINTS[key]
        path = f"/api/1/vehicles/{quote(vin, safe='')}/command/{name}"
        status, answer = self._post(token, path, body(arg))
        woke = False
        if asleep(status, answer):
            if not self.wake(token, vin):
                return Result(False, "The car is asleep and didn't wake up. Try again in a minute.")
            woke = True
            status, answer = self._post(token, path, body(arg))
            if asleep(status, answer):
                return Result(False, "The car went back to sleep. Try again.", woke)
        if status >= 400:
            return Result(False, problem(status, answer), woke)
        r = (answer or {}).get("response") if isinstance(answer, dict) else None
        if isinstance(r, dict) and r.get("result") is False:
            reason = str(r.get("reason") or "").strip().lower()
            if reason in ALREADY:
                return Result(True, "It already was.", woke)
            return Result(False, REASONS.get(reason, "The car said no" + (f" ({reason[:60]})." if reason else ".")), woke)
        if not isinstance(r, dict):
            return Result(False, "Tesla's answer was unreadable.", woke)
        return Result(True, None, woke)

    def health(self) -> bool:
        try:
            status, _ = self.call("GET", f"{self.url}/health", headers={})
        except Exception:  # noqa: BLE001 - any failure is no answer
            return False
        return status == 200

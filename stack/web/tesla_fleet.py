"""Tesla's Fleet API, the few calls Car Command makes: sign-in, the app's domain, the
account's cars and whether each holds the app's key, the signed config that points a car's
stream at the receiver, and the Supercharger bills (charging history and its invoice PDFs).

Every request goes through one `call(method, url, *, headers, data, json) -> (status, body)`,
httpx by default, so tests answer from a queue and nothing here touches the network on its
own. Tokens travel only in headers and form bodies, never in a URL. A refusal raises
TeslaError with the client secret masked: Tesla's error text can echo what it was sent."""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode

from sources import scrub

AUTHORIZE = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
NA = "https://fleet-api.prd.na.vn.cloud.tesla.com"
# vehicle_charging_cmds is the scope Tesla puts its charging history and invoices under.
# It also permits charge commands (start, stop, limits); Car Command sends none.
BILLING_SCOPE = "vehicle_charging_cmds"
SCOPES = f"openid offline_access vehicle_device_data vehicle_location {BILLING_SCOPE}"
# energy_device_data reads a Powerwall's live status and history; Car Command sends no energy command.
# Asked for only by the Energy page's own sign-in, with the vehicle scopes beside it so one token
# covers both: a developer app without Energy Product Data must still be able to sign in for cars.
ENERGY_SCOPE = "energy_device_data"
ENERGY_SCOPES = f"{SCOPES} {ENERGY_SCOPE}"
PARTNER_SCOPES = "openid vehicle_device_data vehicle_location"


class TeslaError(Exception):
    """`public` is the sentence a page or an answer may show: written here, scrubbed of the
    secrets that were sent, never an exception's own text or a trace."""

    def __init__(self, text: str, status: int | None = None):
        super().__init__(text)
        self.status = status
        self.public = str(text)


def _httpx(method, url, *, headers, data=None, json=None):
    import httpx

    r = httpx.request(method, url, headers={"User-Agent": "carcommand/0.1", **headers}, data=data, json=json, timeout=20)
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


def _httpx_raw(method, url, *, headers):
    """A response that is not JSON (an invoice PDF): (status, content type, bytes)."""
    import httpx

    r = httpx.request(method, url, headers={"User-Agent": "carcommand/0.1", **headers}, timeout=30)
    return r.status_code, r.headers.get("content-type", ""), r.content


def granted_scopes(access_token: str | None) -> set[str]:
    """The scopes an access token carries, read from its own payload (a JWT's `scp`, a list
    or a space-separated string). Nothing is verified or sent: it only says whether to ask
    Tesla for something the token could not get. Empty when it cannot be read."""
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return set()
    scp = claims.get("scp") if isinstance(claims, dict) else None
    if isinstance(scp, str):
        return set(scp.split())
    return {s for s in scp if isinstance(s, str)} if isinstance(scp, list) else set()


class Fleet:
    def __init__(self, client_id: str, client_secret: str, call=None, raw=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.call = call or _httpx
        self.raw = raw or _httpx_raw

    # ---- plumbing
    def _send(self, method, url, *, token=None, data=None, json=None, missing_ok=False):
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            status, body = self.call(method, url, headers=headers, data=data, json=json)
        except httpx.HTTPError as err:
            raise TeslaError(f"Tesla did not answer ({type(err).__name__}).") from None
        if missing_ok and status == 404:
            return None
        if not isinstance(body, dict):
            raise TeslaError(f"Tesla answered {status} with no readable body", status)
        if status >= 400:
            reason = body.get("error_description") or body.get("error") or f"status {status}"
            if body.get("error") and body.get("error_description"):
                reason = f"{body['error']}: {body['error_description']}"
            sent = [v for k, v in (data or {}).items() if k in ("client_secret", "refresh_token", "code")]
            raise TeslaError(scrub(f"Tesla refused ({status}): {reason}", self.client_secret, token or "", *sent)[:300], status)
        return body

    def _tokens(self, data: dict) -> dict:
        body = self._send("POST", TOKEN, data=data)
        if not body.get("access_token"):
            raise TeslaError("Tesla answered without a token.")
        return {"access_token": body.get("access_token"), "refresh_token": body.get("refresh_token"), "expires_at": time.time() + float(body.get("expires_in") or 0)}

    # ---- signing in
    def authorize_url(self, redirect_uri: str, state: str, energy: bool = False) -> str:
        params = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": ENERGY_SCOPES if energy else SCOPES, "state": state}
        if energy:
            # An account that consented before is otherwise handed its old grant back, silently
            # without a scope it never granted: Tesla is asked to prompt for the missing one.
            params["prompt_missing_scopes"] = "true"
        return AUTHORIZE + "?" + urlencode(params)

    def exchange(self, code: str, redirect_uri: str) -> dict:
        return self._tokens({"grant_type": "authorization_code", "client_id": self.client_id, "client_secret": self.client_secret, "code": code, "audience": NA, "redirect_uri": redirect_uri})

    def refresh(self, refresh_token: str) -> dict:
        """Tesla's refresh tokens are single use: the caller keeps the new one before using the access token."""
        return self._tokens({"grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": refresh_token})

    def partner_token(self) -> str:
        body = self._send("POST", TOKEN, data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret, "scope": PARTNER_SCOPES, "audience": NA})
        if not body.get("access_token"):
            raise TeslaError("Tesla answered without a token.")
        return body["access_token"]

    # ---- the app's domain
    def register(self, domain: str) -> None:
        self._send("POST", f"{NA}/api/1/partner_accounts", token=self.partner_token(), json={"domain": domain})

    def registered_key(self, domain: str) -> str | None:
        body = self._send("GET", f"{NA}/api/1/partner_accounts/public_key?" + urlencode({"domain": domain}), token=self.partner_token(), missing_ok=True)
        return ((body or {}).get("response") or {}).get("public_key")

    # ---- the account
    def region(self, access: str) -> str:
        body = self._send("GET", f"{NA}/api/1/users/region", token=access)
        return (body.get("response") or {}).get("fleet_api_base_url") or NA

    def vehicles(self, access: str, base: str) -> list[dict]:
        body = self._send("GET", f"{base}/api/1/vehicles", token=access)
        return [{"vin": v.get("vin"), "name": v.get("display_name") or v.get("vin"), "state": v.get("state")} for v in body.get("response") or [] if v.get("vin")]

    def vehicle_config(self, access: str, base: str, vin: str) -> dict:
        """The car's vehicle_config and vehicle_state, nothing else (no location). This does not
        wake a car: one asleep or offline answers 408 (raised as TeslaError with that status).
        Callers ask only a car the account's list says is online."""
        endpoints = quote("vehicle_config;vehicle_state", safe="")
        return self._send("GET", f"{base}/api/1/vehicles/{quote(vin, safe='')}/vehicle_data?endpoints={endpoints}", token=access)

    # ---- Supercharger bills
    def charging_history(self, access: str, base: str, vin: str, start: str | None = None, page: int = 1, size: int = 50, order: str = "ASC") -> dict:
        """One page of the account's charging history for one car: Tesla's own
        page as it came, {"data": [...], "totalResults": n, "hasMoreData": bool}, which some
        regions wrap in "response". `start` is an ISO time to read from. The VIN and the
        page are the only things in the URL; the token is in the header."""
        query = {"vin": vin, "pageNo": page, "pageSize": size, "sortOrder": order}
        if start:
            query["startTime"] = start
        body = self._send("GET", f"{base}/api/1/dx/charging/history?" + urlencode(query), token=access)
        return body.get("response") if isinstance(body.get("response"), dict) else body

    def charging_invoice(self, access: str, base: str, content_id: str) -> bytes:
        """An invoice PDF by the contentId the history gave it."""
        import httpx

        url = f"{base}/api/1/dx/charging/invoice/{quote(content_id, safe='')}"
        try:
            status, kind, data = self.raw("GET", url, headers={"Authorization": f"Bearer {access}"})
        except httpx.HTTPError as err:
            raise TeslaError(f"Tesla did not answer ({type(err).__name__}).") from None
        if status >= 400 or not data:
            raise TeslaError(f"Tesla refused the invoice ({status}).", status)
        if "pdf" not in (kind or "").lower() and not data.startswith(b"%PDF"):
            raise TeslaError("Tesla's answer was not a PDF.", status)
        return data

    # ---- streaming: the signed config that points a car at the receiver
    def telemetry_create(self, access: str, base: str, vins: list[str], token: str) -> dict:
        """Hand Tesla the signed config (tesla_jws.token) for these cars. Tesla answers how many
        took it and, per reason (missing_key, unsupported_firmware, max_configs, ...), the ones it skipped."""
        body = self._send("POST", f"{base}/api/1/vehicles/fleet_telemetry_config_jws", token=access, json={"vins": list(vins), "token": token})
        r = body.get("response") or {}
        skipped = {k: list(v) for k, v in (r.get("skipped_vehicles") or {}).items() if v}
        return {"updated": int(r.get("updated_vehicles") or 0), "skipped": skipped}

    def telemetry_config(self, access: str, base: str, vin: str) -> dict | None:
        """The car's config as Tesla holds it: whether the car has taken it (synced), whether it
        already streams to as many apps as it can, whether it still holds the key. None when
        Tesla holds no config for it (never made, or removed: a key taken out of the car, the
        billing limit reached, access revoked)."""
        body = self._send("GET", f"{base}/api/1/vehicles/{quote(vin, safe='')}/fleet_telemetry_config", token=access, missing_ok=True)
        r = (body or {}).get("response") or {}
        config = r.get("config")
        if not config:
            return None
        return {
            "synced": bool(r.get("synced")),
            "limit_reached": bool(r.get("limit_reached")),
            "key_paired": r.get("key_paired"),
            "hostname": config.get("hostname"),
            "port": config.get("port"),
            "exp": config.get("exp"),
            "fields": config.get("fields"),
        }

    def telemetry_delete(self, access: str, base: str, vin: str) -> None:
        self._send("DELETE", f"{base}/api/1/vehicles/{quote(vin, safe='')}/fleet_telemetry_config", token=access, missing_ok=True)

    def telemetry_errors(self, domain: str) -> list[dict]:
        """What cars reported after taking a config for this domain (a receiver they could not
        reach, a certificate they would not trust): [{vin, name, error, at}], newest first."""
        body = self._send("GET", f"{NA}/api/1/partner_accounts/fleet_telemetry_errors?" + urlencode({"domain": domain}), token=self.partner_token(), missing_ok=True)
        r = (body or {}).get("response")
        rows = r.get("fleet_telemetry_errors") if isinstance(r, dict) else r
        out = []
        for e in rows if isinstance(rows, list) else []:
            if isinstance(e, dict):
                out.append({"vin": e.get("vin"), "name": e.get("name") or "", "error": str(e.get("error") or e.get("body") or "")[:300], "at": e.get("created_at") or e.get("createdAt")})
        return sorted(out, key=lambda e: str(e["at"] or ""), reverse=True)

    def fleet_status(self, access: str, base: str, vins: list[str]) -> dict:
        body = self._send("POST", f"{base}/api/1/vehicles/fleet_status", token=access, json={"vins": list(vins)})
        r = body.get("response") or {}
        paired, info = set(r.get("key_paired_vins") or []), r.get("vehicle_info") or {}
        return {
            vin: {
                "paired": vin in paired,
                "firmware": (info.get(vin) or {}).get("firmware_version"),
                "keys": (info.get(vin) or {}).get("total_number_of_keys"),
                "telemetry": (info.get(vin) or {}).get("fleet_telemetry_version"),
            }
            for vin in vins
        }

    # ---- Powerwall: the account's energy sites
    @staticmethod
    def _site(site_id) -> str:
        """The id for a URL: digits only, so nothing but a number is ever put in a path."""
        if not re.fullmatch(r"\d{1,20}", str(site_id or "")):
            raise TeslaError("That is not an energy site id.")
        return str(site_id)

    def energy_sites(self, access: str, base: str) -> list[dict]:
        """The energy sites on the account ([{site_id, name, kind}]); vehicles are skipped."""
        body = self._send("GET", f"{base}/api/1/products", token=access)
        return [
            {"site_id": str(p["energy_site_id"]), "name": p.get("site_name") or "", "kind": p.get("resource_type") or ""}
            for p in body.get("response") or []
            if isinstance(p, dict) and p.get("energy_site_id")
        ]

    def energy_live(self, access: str, base: str, site_id) -> dict:
        """The site's live_status as Tesla gave it (solar_power, battery_power, grid_power,
        load_power, percentage_charged, grid_status, timestamp)."""
        body = self._send("GET", f"{base}/api/1/energy_sites/{self._site(site_id)}/live_status", token=access)
        return body.get("response") or {}

    def energy_installed(self, access: str, base: str, site_id) -> str | None:
        """The day the site was installed (YYYY-MM-DD), where history starts."""
        body = self._send("GET", f"{base}/api/1/energy_sites/{self._site(site_id)}/site_info", token=access)
        text = str((body.get("response") or {}).get("installation_date") or "")
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None

    def energy_power_day(self, access: str, base: str, site_id, day: date, tz: str) -> list[dict]:
        """One local day of the site's power in five-minute samples (calendar_history, kind
        power): [{timestamp, solar_power, battery_power, grid_power, ...}], empty for a day
        with none. One day a request keeps each answer small and the backfill resumable."""
        zone = ZoneInfo(tz)
        first = datetime(day.year, day.month, day.day, tzinfo=zone)
        last = first + timedelta(days=1) - timedelta(seconds=1)
        query = urlencode({"kind": "power", "period": "day", "start_date": first.isoformat(), "end_date": last.isoformat(), "time_zone": tz})
        body = self._send("GET", f"{base}/api/1/energy_sites/{self._site(site_id)}/calendar_history?{query}", token=access)
        series = (body.get("response") or {}).get("time_series")
        return [p for p in series if isinstance(p, dict)] if isinstance(series, list) else []

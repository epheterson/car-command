"""Events worth telling a person about, derived from two successive live snapshots.

Pure: `events(prev, now)` returns a list of messages. Delivery (Discord) is a
separate, tiny function so the logic is testable without a network.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
import urllib.parse
import urllib.request

import identity
from places import car_at_home

LOG = logging.getLogger("carcommand.notify")
LOW_BATTERY_PCT = 20.0


def _n(x, nd=0):
    return "?" if x is None else f"{x:,.{nd}f}"


def event_kinds(prev: dict | None, now: dict) -> list[tuple[str, str]]:
    """Messages for what changed between `prev` and `now` (both live_vehicle() dicts)."""
    if prev is None:
        return []
    out: list[str] = []
    name = now.get("name", "Car")
    where = now.get("place") or now.get("address") or "an unnamed spot"
    ps, ns = prev.get("status"), now.get("status")

    if ps != "Charging" and ns == "Charging":
        out.append(("charge_start", f"{name} started charging at {where}: {_n(now.get('soc'))}% now, limit {_n(now.get('charge_target'))}%, {_n(now.get('charging_power_kw'), 1)} kW."))
    if ps == "Charging" and ns != "Charging":
        out.append(("charge_done", f"{name} finished charging at {where}: {_n(now.get('soc'))}%, {_n(now.get('range_mi'))} mi of range."))
    if ps == "Driving" and ns != "Driving":
        miles = (now.get("odometer_mi") or 0) - (prev.get("odometer_mi") or 0)
        out.append(("parked", f"{name} parked at {where}: {_n(now.get('soc'))}%, {_n(now.get('range_mi'))} mi left" + (f", {_n(miles, 1)} mi since it last woke." if miles > 0.2 else ".")))
    if ps != "Driving" and ns == "Driving":
        out.append(("driving", f"{name} is driving, leaving {prev.get('place') or prev.get('address') or 'an unnamed spot'} at {_n(now.get('soc'))}%."))

    pp, np_ = prev.get("place"), now.get("place")
    if pp != np_ and np_ and ns != "Driving":
        out.append(("arrived", f"{name} arrived at {np_}."))

    soc_prev, soc_now = prev.get("soc"), now.get("soc")
    if soc_prev is not None and soc_now is not None and soc_prev >= LOW_BATTERY_PCT > soc_now:
        out.append(("low_battery", f"{name} is down to {_n(soc_now)}% ({_n(now.get('range_mi'))} mi)."))

    # `is False`, not falsy. These fields are tri-state: None means the car has not said,
    # and a car that has stopped answering is not a car that has been left open. Reading
    # unknown as unlocked is how a logger wakes someone at night about nothing.
    if prev.get("locked") is True and now.get("locked") is False and ns in ("Sleeping", "Idle"):
        out.append(("unlocked", f"{name} is unlocked at {where}."))
    if now.get("doors_open") is True and prev.get("doors_open") is not True and ns in ("Sleeping", "Idle"):
        out.append(("door", f"A door on {name} is open at {where}."))
    if now.get("windows_open") is True and prev.get("windows_open") is not True and ns in ("Sleeping", "Idle"):
        out.append(("window", f"A window on {name} is open at {where}."))
    # The car's guard mode (Sentry, Halo Secure) reacting to something, once when it starts; the level is not carried, the
    # fact that it is happening is what the message is for.
    if now.get("sentry_threat") is True and prev.get("sentry_threat") is not True:
        out.append(("sentry", f"{identity.guard_name(identity.make_of(now.get('vin'), now.get('make')))} on {name} is reacting to something at {where}."))
    if now.get("battery_warning") is True and prev.get("battery_warning") is not True:
        out.append(("battery_warning", f"{name} is flagging a battery health warning."))
    if now.get("update_available") is True and prev.get("update_available") is not True:
        out.append(("update", f"A software update is waiting on {name}."))
    # The car's own tire warning, once when it comes on. Not a threshold applied here:
    # the proto carries no target pressure, and the car knows its own placard.
    if now.get("tire_warning") is True and prev.get("tire_warning") is not True:
        psi = now.get("tires_psi") or {}
        low = min(psi.values()) if psi else None
        out.append(("tire", f"{name} is warning about a tire" + (f" ({_n(low, 1)} psi at the lowest)." if low is not None else ".")))
    out.extend(("tire", t) for t in tire_limits(prev, now, TIRE_LOW, TIRE_HIGH))
    return out


TIRE_LOW: float | None = None  # from Settings, applied at startup and on save
TIRE_HIGH: float | None = None
WHEEL = {"front_left": "front left", "front_right": "front right", "rear_left": "rear left", "rear_right": "rear right"}


def tire_limits(prev: dict, now: dict, low: float | None, high: float | None) -> list[str]:
    """Your own pressure limits, on top of the car's warning: one message per tire the
    moment it crosses, none while it stays there, none if no limit is set."""
    if low is None and high is None:
        return []
    before, after = prev.get("tires_psi") or {}, now.get("tires_psi") or {}
    name = now.get("name") or "The car"
    out = []
    for wheel, psi in after.items():
        was = before.get(wheel)
        if low is not None and psi < low and (was is None or was >= low):
            out.append(f"{name}'s {WHEEL.get(wheel, wheel)} tire is at {_n(psi, 1)} psi, below your {_n(low, 1)} psi limit.")
        if high is not None and psi > high and (was is None or was <= high):
            out.append(f"{name}'s {WHEEL.get(wheel, wheel)} tire is at {_n(psi, 1)} psi, above your {_n(high, 1)} psi limit.")
    return out


# The plug-in reminder. Fires once a day, after a set local time, when a car is home,
# parked, below a level, and the car has said it is not plugged in. "Said" matters:
# `plugged` is tri-state, and a car that has not reported a cable is not a car left
# unplugged. That is the same rule as the unlocked alert and for the same reason.
PLUG_REMINDER_AT = time(21, 0)
PLUG_REMINDER_BELOW_PCT = 50.0


KINDS = {
    "charge_start": "Charging started",
    "charge_done": "Charging finished",
    "parked": "Parked",
    "driving": "Set off",
    "arrived": "Arrived at a place",
    "low_battery": "Battery low",
    "unlocked": "Left unlocked",
    "door": "Door open",
    "window": "Window open",
    "sentry": "Sentry reacting",
    "battery_warning": "Battery health warning",
    "update": "Software update waiting",
    "tire": "Tire warning",
    "connector": "Connector down or back",
    "drive_summary": "Drive summary",
    "daily_summary": "Daily summary",
    "plug_reminder": "Plug-in reminder",
    "login": "Sign-in",
    "data": "Data check",
}


# Settings › Alerts lists the kinds in these groups, each kind in exactly one (a test holds it).
GROUPS = (
    ("Charging", ("charge_start", "charge_done", "low_battery", "plug_reminder")),
    ("Driving", ("driving", "arrived", "parked")),
    ("Security", ("unlocked", "door", "window", "sentry")),
    ("Car health", ("battery_warning", "tire", "update")),
    ("Summaries", ("drive_summary", "daily_summary")),
    ("System", ("connector", "data", "login")),
)

CHANNELS = {"discord": "Discord", "telegram": "Telegram", "email": "E-mail", "pushover": "Pushover"}


def kinds_for(makes) -> dict[str, str]:
    """KINDS as Settings shows them: the guard alert under the name of the cars' own mode."""
    return {**KINDS, "sentry": f"{identity.guard_label(makes)} reacting"}


def routes_from(st: dict) -> dict[str, set[str]]:
    """Which channels each alert kind goes to, from Settings. `alert_routes` is kind -> list
    of channel names; a kind that is missing goes everywhere, one with an empty list is
    off. Installs from before routing had `muted_alerts`, a list of kinds switched off;
    that still reads the same way."""
    routes = st.get("alert_routes")
    if isinstance(routes, dict):
        return {k: {c for c in v if c in CHANNELS} for k, v in routes.items() if k in KINDS and isinstance(v, list)}
    return {k: set() for k in (st.get("muted_alerts") or []) if k in KINDS}


def events(prev: dict | None, now: dict) -> list[str]:
    """The messages alone, for callers that do not filter by kind."""
    return [text for _kind, text in event_kinds(prev, now)]


def plug_in_reminder(car: dict, now_local: datetime, last_sent: date | None, at: time = PLUG_REMINDER_AT, below: float = PLUG_REMINDER_BELOW_PCT) -> str | None:
    """The reminder text if one is due now, else None. `last_sent` is the local date it
    last went out for this car, kept in the database so a restart cannot repeat it."""
    if now_local.time() < at or last_sent == now_local.date():
        return None
    if car.get("plugged") is not False:
        return None
    if not car_at_home(car):
        return None
    if car.get("status") not in ("Sleeping", "Idle"):
        return None
    soc = car.get("soc")
    if soc is None or soc >= below:
        return None
    name = car.get("name", "Car")
    return f"{name} is home at {_n(soc)}% and not plugged in."


def connector_down(consecutive_failures: int, threshold: int = 10) -> bool:
    """True exactly once, when failures reach the threshold (ten minutes at a one-minute tick)."""
    return consecutive_failures == threshold


def send_telegram(bot_token: str | None, chat_id: str | None, text: str) -> bool:
    """Telegram Bot API sendMessage; the token never appears in logs."""
    if not bot_token or not chat_id:
        return False
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "carcommand/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("telegram send failed: %s", err.__class__.__name__)
        return False


def send_discord(webhook_url: str | None, text: str) -> bool:
    if not webhook_url:
        return False
    body = json.dumps({"content": text[:1900], "username": "Car Command"}).encode()
    req = urllib.request.Request(webhook_url, data=body, headers={"Content-Type": "application/json", "User-Agent": "carcommand/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("discord send failed: %s", err.__class__.__name__)
        return False


def parse_smtp_url(url: str | None) -> dict | None:
    """smtp://user:pass@host:587 (STARTTLS) or smtps://user:pass@host:465 (TLS). None when unset or malformed."""
    if not url:
        return None
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("smtp", "smtps") or not u.hostname:
        return None
    return {
        "tls": u.scheme == "smtps",
        "host": u.hostname,
        "port": u.port or (465 if u.scheme == "smtps" else 587),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
    }


def send_email(smtp_url: str | None, to: str | None, sender: str | None, subject: str, text: str) -> bool:
    """One plain-text mail per event. The password
    lives in the URL in the environment and never in a log line."""
    conf = parse_smtp_url(smtp_url)
    if conf is None or not to:
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = sender or conf["user"] or f"carcommand@{conf['host']}"
    msg["To"] = to
    msg.set_content(text[:8000])
    try:
        if conf["tls"]:
            server = smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=15)
        else:
            server = smtplib.SMTP(conf["host"], conf["port"], timeout=15)
            server.starttls()
        with server:
            if conf["user"]:
                server.login(conf["user"], conf["password"])
            server.send_message(msg)
        return True
    except Exception as err:  # noqa: BLE001
        LOG.warning("email send failed: %s", err.__class__.__name__)
        return False


def send_pushover(app_token: str | None, user_key: str | None, text: str, title: str = "Car Command") -> bool:
    """Pushover: an app token and a user key."""
    if not app_token or not user_key:
        return False
    body = urllib.parse.urlencode({"token": app_token, "user": user_key, "title": title[:100], "message": text[:1024]}).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "carcommand/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as err:  # noqa: BLE001
        LOG.warning("pushover send failed: %s", err.__class__.__name__)
        return False

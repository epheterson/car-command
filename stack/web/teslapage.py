"""Connect Tesla as a checklist. Each step is done, next, waiting on an earlier one, or a
problem with its reason. A step can only be next, done or a problem when every step before
it is done, so the page never shows a tick that something earlier undoes."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import tesla_net

LOG = logging.getLogger("carcommand.web")

TITLES = {
    "app": "The app's keys",
    "domain": "Publish the key and register the domain",
    "signin": "Sign in with Tesla",
    "keys": "Add the key to each car",
    "stream": "Stream from each car",
}

# Why Tesla skipped a car when it was asked to stream, and what to do about it.
SKIPPED = {
    "missing_key": "{name} does not hold the app's key yet: add it in step 4, then Start again.",
    "unsupported_firmware": "{name}'s software is too old to stream; Tesla needs 2024.26 or later. It updates over Wi-Fi; Start again after.",
    "unsupported_hardware": "{name} is too old for Tesla's streaming (Model S and X built before 2021).",
    "max_configs": "{name} already streams to five apps, the most a car allows. Remove one of them from the car's keys, then Start again.",
}

RECEIVER_FIX = {
    "unreachable": "Is the tesla profile up? `make tesla-up` starts it and `make tesla-logs` says why it is not.",
    "certificate": "HTTPS certificates must be on for the tailnet (Tailscale admin › DNS); `make tesla-restart` fetches a new one.",
    "expiring": "It renews itself a month ahead, so a renewal did not reach the receiver: `make tesla-restart`.",
}


def skipped_text(reason: str, name: str) -> str:
    return SKIPPED.get(reason, "Tesla skipped {name}: " + reason.replace("_", " ") + ".").format(name=name)


def stream_cars(f: dict) -> list[dict]:
    """Per car on the account: whether it streams, and the one thing to say about it."""
    out = []
    for c in f.get("cars") or []:
        s = {**c, **((f.get("stream_cars") or {}).get(c["vin"]) or {})}
        name = c.get("name") or c["vin"]
        if not s.get("on"):
            s["said"] = None
        elif not c.get("paired"):
            s["said"] = f"{name} no longer holds the app's key, so Tesla dropped its stream. Add the key again in step 4, then Start."
        elif s.get("config") is None and s.get("config_known"):
            s["said"] = (
                f"Tesla holds no stream for {name} any more. Tesla removes every car's when the app reaches its billing limit, or when the owner takes the app's access away: raise the limit at developer.tesla.com, then Start again."
            )
        elif (s.get("config") or {}).get("limit_reached"):
            s["said"] = SKIPPED["max_configs"].format(name=name)
        elif s.get("config") and not s["config"].get("synced") and not s.get("last_at"):
            # a car already heard from streams: a config sent again (a new field list) is taken
            # at its next wake, and until then the one it holds keeps streaming
            s["said"] = f"Waiting for {name} to take it: a car does when it next wakes and is online."
        else:
            s["said"] = None
        s["outdated"] = bool(s.get("on") and s.get("config")) and outdated(s["config"], s.get("sent"))
        s["trouble"] = error_summary(s.get("errors") or [], name)
        out.append(s)
    return out


# What a car's telemetry error means, by the first pattern its text matches.
ERROR_KINDS = (
    ("unreachable", re.compile(r"dial tcp|i/o timeout|connection refused|no such host|connection reset|webconnection", re.I), "Tesla couldn't reach the receiver"),
    ("certificate", re.compile(r"x509|certificate|tls", re.I), "The car didn't trust the receiver's certificate"),
)
ERROR_WINDOW = timedelta(hours=24)


def _at(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def error_summary(errors: list[dict], name: str, now: datetime | None = None) -> list[dict]:
    """A car's errors as one line per kind: what happened, how often in the last day, the
    newest, and its raw text (for an (i)) with the app's id taken out. An error with no time
    (Tesla's answer to this page's own question) is current; one older than a day is gone."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for e in errors:
        at = _at(e["at"]) if e.get("at") else None
        if at is not None and now - at > ERROR_WINDOW:
            continue
        body, app = str(e.get("body") or ""), str(e.get("name") or "")
        if app == "Tesla" and at is None:
            kind, what = "asked", "Tesla didn't answer about its config"
        else:
            if app:
                body = body.replace(app, "…")
            kind, what = next(((k, w) for k, rx, w in ERROR_KINDS if rx.search(body)), ("other", f"{name} reported an error"))
        row = out.setdefault(kind, {"kind": kind, "what": what, "n": 0, "last": None, "raw": body})
        row["n"] += 1
        if at is not None and (row["last"] is None or at > row["last"]):
            row["last"], row["raw"] = at, body
    return sorted(out.values(), key=lambda r: r["last"] or now, reverse=True)


def outdated(config: dict | None, sent: str | None) -> bool:
    """Whether a started car's config asks for other fields than the code does now: by the
    fields Tesla says the car holds, or, when Tesla does not say, by the field list the car
    was last sent (tesla_net.fields_version, kept at each Start; none kept is a config from
    before the list was kept, so older than any change since)."""
    held = tesla_net.sent_fields(config)
    if held is not None:
        return held != tesla_net.FIELDS or tesla_net.sent_deltas(config) != tesla_net.DELTAS
    return sent != tesla_net.fields_version()


def to_update(started: list[str], sent: dict[str, str | None], tried: dict[str, str | None], now: str | None = None) -> list[str]:
    """The started cars whose config the background check sends again: last sent another
    field list than the code's, and not already tried with this one (once per change, so a
    car Tesla refuses is not asked every start). Never a car that was not started."""
    now = now or tesla_net.fields_version()
    return [vin for vin in started if sent.get(vin) != now and tried.get(vin) != now]


def _stream(f: dict) -> tuple[bool, str | None, str]:
    """The streaming step's own result: (done, problem, what to do next)."""
    host = f.get("stream_host")
    if not host:
        return False, None, "Start the stream's Funnel node and set TESLA_STREAM_HOST to its name, carcommand-stream.<your tailnet>.ts.net (docs/tesla-stream.md)."
    if not f.get("stream_host_ok"):
        return False, f"TESLA_STREAM_HOST is {host!r}; it should be a name alone, like carcommand-stream.<your tailnet>.ts.net, with no https:// or port.", ""
    if "tesla" in host.lower():
        return (
            False,
            f"Tesla refuses a domain with \"tesla\" in it, so {host} cannot be the stream address. Name the Funnel node something else (carcommand-stream is the default) and set TESLA_STREAM_HOST to match.",
            "",
        )
    if f.get("stream_public_error") or f.get("stream_register_error"):
        return False, f.get("stream_public_error") or f.get("stream_register_error"), ""
    receiver = f.get("receiver") or {}
    if receiver.get("problem"):
        return False, f"{receiver.get('error')} {RECEIVER_FIX[receiver['problem']]}", ""
    if not f.get("stream_registered"):
        return False, None, "Tell Tesla about the stream address (the cars only stream to a registered domain)."
    cars = stream_cars(f)
    on = [c for c in cars if c.get("on")]
    trouble = next((c["said"] for c in on if c["said"] and not c["said"].startswith("Waiting")), None)
    if trouble:
        return False, trouble, ""
    if not on:
        return False, None, "Start the cars you want streamed. Each takes the config the next time it wakes."
    waiting = next((c["said"] for c in on if c["said"]), None)
    return waiting is None, None, waiting or ""


def key_link(domain: str, vin: str) -> str:
    """Opened on the owner's phone, Tesla's app offers to add this app's key to the car."""
    return f"https://tesla.com/_ak/{domain}?vin={vin}"


def _own(f: dict) -> list[tuple[str, bool, str | None, str]]:
    """Per step: (id, its own result is done, its problem, what to do while it is next)."""
    cars = f.get("cars") or []
    paired = sum(1 for c in cars if c.get("paired"))
    keys_problem = "Tesla lists no cars on this account." if f.get("account") and not cars and not f.get("account_error") else None
    lost = [c.get("name") or c["vin"] for c in cars if not c.get("paired") and ((f.get("stream_cars") or {}).get(c["vin"]) or {}).get("on")]
    if lost and not keys_problem:
        keys_problem = f"{', '.join(lost)} no longer {'holds' if len(lost) == 1 else 'hold'} the app's key, so Tesla dropped the stream. Open the link below on your phone, near the car, then Start again in step 5."
    stream_ok, stream_problem, stream_todo = _stream(f)
    return [
        ("app", bool(f.get("app_ok")), f.get("app_error"), ""),
        ("domain", bool(f.get("registered")), f.get("public_error") or f.get("register_error"), "Publish the app's key on the domain and tell Tesla about it."),
        ("signin", bool(f.get("account")) and not f.get("account_error"), f.get("account_error"), "Sign in to the Tesla account the cars are on."),
        ("keys", bool(cars) and paired == len(cars), keys_problem, f"{paired} of {len(cars)} cars have the key. Open each car's link on your phone, near the car."),
        ("stream", stream_ok, stream_problem, stream_todo),
    ]


def steps(f: dict) -> list[dict]:
    out, clear = [], True
    for sid, ok, problem, todo in _own(f):
        if not clear:
            state, detail = "waiting", "After the step above."
        elif problem:
            state, detail = "problem", problem
        elif ok:
            state, detail = "done", ""
        else:
            state, detail = "next", todo
        out.append({"id": sid, "title": TITLES[sid], "state": state, "detail": detail})
        clear = clear and state == "done"
    return out


def tried_after(before: str | None, now: str, transient: bool) -> str:
    """What to keep as a car's try with field list `now`: the list itself (tried, not again),
    except when Tesla could not be reached or failed (no answer, 5xx) on a first try, which
    leaves it one more, at the next start ("<now>:retry", which to_update still sends)."""
    if transient and before != f"{now}:retry":
        return f"{now}:retry"
    return now


class Refresher(threading.Thread):
    """Asks Tesla for the page's facts off the page path: once `warm_s` after start, then only
    when a page read found an answer missing or past its ttl (wake). Each answer keeps its own
    ttl, so a round asks Tesla no more than opening the page did; nobody on the page, no calls."""

    def __init__(self, round_: Callable, warm_s: float = 30) -> None:
        super().__init__(daemon=True, name="tesla-facts")
        self.round_, self.warm_s = round_, warm_s
        self.woken, self.stopped = threading.Event(), threading.Event()
        self.rounds = 0

    def wake(self) -> None:
        self.woken.set()

    def stop(self) -> None:
        self.stopped.set()
        self.woken.set()

    def run(self) -> None:
        if self.stopped.wait(self.warm_s):
            return
        self.woken.set()
        while self.woken.wait() and not self.stopped.is_set():
            self.woken.clear()
            try:
                self.round_()
            except Exception as err:  # noqa: BLE001 - the next page read wakes it again
                LOG.warning("tesla facts: %s", err)
            self.rounds += 1


def site_hints(public_url: str, root: str, env_domain: str = "", env_redirect: str = "") -> dict:
    """The domain and redirect address Connect Tesla shows for the developer app: what .env already sets (TESLA_DOMAIN), else the
    public address (CC_PUBLIC_URL). Never the address the page was opened on: that is a home address over plain http, which Tesla
    refuses, so with neither set the domain is empty and the page asks for the public hostname."""
    from urllib.parse import urlparse

    pub = urlparse(public_url) if public_url else None
    domain = env_domain or (pub.hostname if pub and pub.hostname else "")
    path = (pub.path.rstrip("/") if pub and pub.hostname else root) or ""
    redirect = env_redirect or (f"https://{domain}{path}/settings/sources/tesla/callback" if domain else "")
    return {"domain": domain, "redirect": redirect}

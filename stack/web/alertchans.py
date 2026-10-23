"""Alert channels added in Settings › Alerts: Discord, Telegram, e-mail, Pushover and a webhook.

One channel of each kind, kept in cc_state as `alert_channel:<kind>` with its fields sealed together
(sources.Vault, bound to that key) and only the last four of its first field beside them in the
clear. A channel set in .env wins over a stored one of its kind and cannot be removed here. Pure
apart from the connection the callers pass in; the sending itself is Recorder.send, so a message
from an event and a test-send take the same road.
"""

from __future__ import annotations

import json
import urllib.parse

from notify import CHANNELS, parse_smtp_url

STATE_PREFIX = "alert_channel:"

# kind -> (field, label, required, hint). The first field is the one whose last four are shown back.
FIELDS: dict[str, tuple[tuple[str, str, bool, str], ...]] = {
    "discord": (("url", "Webhook URL", True, "https://discord.com/api/webhooks/…"),),
    "telegram": (("token", "Bot token", True, "123456:ABC…"), ("chat_id", "Chat id", True, "123456789")),
    "email": (("smtp_url", "SMTP URL", True, "smtps://user:password@smtp.example.com:465"), ("to", "To", True, "you@example.com"), ("from", "From", False, "optional")),
    "pushover": (("token", "App token", True, ""), ("user", "User key", True, "")),
    "webhook": (("url", "URL", True, "https://"), ("token", "Bearer token", False, "optional")),
}
assert set(FIELDS) == set(CHANNELS)  # a new channel kind has its form here too


class ChannelError(Exception):
    pass


def _web(url: str, https_only: bool) -> bool:
    u = urllib.parse.urlsplit(url)
    return bool(u.hostname) and u.scheme in (("https",) if https_only else ("http", "https"))


def check(kind: str, form) -> dict:
    """The fields a form posted for one kind, trimmed and checked; ChannelError says what is wrong."""
    if kind not in FIELDS:
        raise ChannelError("Not a channel kind.")
    out: dict[str, str] = {}
    for field, label, required, _hint in FIELDS[kind]:
        value = " ".join(str(form.get(f"{kind}_{field}") or "").split())[:500]
        if required and not value:
            raise ChannelError(f"{label} is empty.")
        out[field] = value
    if kind == "discord" and not _web(out["url"], True):
        raise ChannelError("A Discord webhook is an https address.")
    if kind == "webhook" and not _web(out["url"], False):
        raise ChannelError("A webhook is an http or https address.")
    if kind == "email" and (parse_smtp_url(out["smtp_url"]) is None or "@" not in out["to"]):
        raise ChannelError("Use an smtp:// or smtps:// address, and a recipient with an @.")
    return out


def save(conn, vault, kind: str, conf: dict) -> None:
    from sources import last4

    first = FIELDS[kind][0][0]
    key = STATE_PREFIX + kind
    value = json.dumps({"sealed": vault.seal(json.dumps(conf), "state", key).decode(), "last4": last4(conf[first])})
    conn.execute("INSERT INTO cc_state (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def forget(conn, kind: str) -> None:
    conn.execute("DELETE FROM cc_state WHERE key = %s", (STATE_PREFIX + kind,))


def _rows(conn) -> list[tuple[str, dict]]:
    out = []
    for r in conn.execute("SELECT key, value FROM cc_state WHERE key LIKE %s", (STATE_PREFIX + "%",)).fetchall():
        kind = r["key"][len(STATE_PREFIX) :]
        try:
            kept = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        if kind in FIELDS and isinstance(kept, dict):
            out.append((kind, kept))
    return out


def hints(conn) -> dict[str, str]:
    """kind -> "…1234" for each stored channel, read without opening any."""
    return {kind: kept.get("last4") or "…" for kind, kept in _rows(conn)}


def load(conn, vault) -> dict[str, dict]:
    """kind -> fields, for each stored channel that opens with this key (one sealed with another does not)."""
    out = {}
    for kind, kept in _rows(conn):
        opened = vault.open(str(kept.get("sealed") or "").encode(), "state", STATE_PREFIX + kind) if kept.get("sealed") else None
        try:
            conf = json.loads(opened) if opened else None
        except ValueError:
            conf = None
        if isinstance(conf, dict):
            out[kind] = conf
    return out

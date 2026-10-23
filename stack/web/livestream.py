"""The live Now page's wire: which cards changed since the last look, and how a server-sent
event is written. Pure, so the stream in app.py only loops, renders and waits."""

from __future__ import annotations

import json

# A comment line: nothing for the page, but it keeps a proxy from closing a quiet stream.
HEARTBEAT = ": keep\n\n"


def sse(event: str, data: dict) -> str:
    """One event. The JSON is on one line, so markup with newlines cannot break the frame."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def changed(prev: dict[str, str], cards: dict[str, dict]) -> tuple[list[dict], dict[str, str]]:
    """The cards whose markup differs from what was last sent, and the new record of it.
    A card no longer offered is forgotten: if it comes back, it is sent again."""
    sent = [{"vin": vin, "html": c["html"], "lat": c.get("lat"), "lon": c.get("lon"), "heading": c.get("heading")} for vin, c in cards.items() if prev.get(vin) != c["html"]]
    return sent, {vin: c["html"] for vin, c in cards.items()}


def changed_states(prev: dict[str, str], states: dict[str, dict]) -> tuple[list[dict], dict[str, str]]:
    """The states whose JSON differs from what was last sent, and the new record of it, for the
    API stream. As with the cards, a car no longer offered is forgotten and sent again if it returns."""
    now = {vin: json.dumps(s, sort_keys=True, separators=(",", ":")) for vin, s in states.items()}
    return [states[vin] for vin, text in now.items() if prev.get(vin) != text], now

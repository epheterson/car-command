"""Who and where, in words, for the alerts that say a sign-in happened: "Chrome on a Mac", "your network".

A user-agent string and a Docker gateway address mean nothing to the person reading an alert. The audit log keeps the raw
address; the message a person reads says the browser and the kind of place."""

from __future__ import annotations

import ipaddress
import re

_BROWSERS = (
    ("Edg/", "Edge"),
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_TOOLS = (("curl/", "curl"), ("python-", "a script"), ("Python", "a script"), ("Go-http-client", "a script"), ("okhttp", "an app"), ("CFNetwork", "an app"), ("Dart/", "an app"))
_SYSTEMS = (
    ("iPhone", "an iPhone"),
    ("iPad", "an iPad"),
    ("Android", None),
    ("CrOS", "a Chromebook"),
    ("Macintosh", "a Mac"),
    ("Mac OS X", "a Mac"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
    ("X11", "Linux"),
)


# Inside the house: private ranges, the carrier-grade and tailnet range, and IPv6 unique-local addresses. Not "is_private":
# that also covers the documentation ranges, which a test (and a reader of the docs) uses for a public address.
_HOME = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"))


def browser(agent: str | None) -> str:
    """ "Chrome on a Mac" from a user-agent string; "" when it says nothing a person would recognise."""
    ua = (agent or "").strip()
    if not ua:
        return ""
    for mark, name in _TOOLS:
        if ua.startswith(mark) or (mark in ("okhttp", "CFNetwork", "Dart/") and mark in ua):
            return name
    name = next((n for m, n in _BROWSERS if m in ua), "")
    system = ""
    for mark, label in _SYSTEMS:
        if mark in ua:
            system = label or ("an Android phone" if "Mobile" in ua else "an Android tablet")
            break
    if name and system:
        return f"{name} on {system}"
    return name or system


def where(address: str | None) -> str:
    """ "your network" for an address inside the house (or the Docker gateway every house-side browser appears to come from);
    a public address as it is. `1.2.3.4 via 10.0.0.1` is the first: the proxy in between is not where anyone sits."""
    first = (address or "").split(" via ")[0].strip()
    try:
        ip = ipaddress.ip_address(first)
    except ValueError:
        return "" if first in ("", "?") else first  # a name, not an address: said as it is
    if ip.is_loopback or ip.is_link_local or any(ip in net for net in _HOME if net.version == ip.version):
        return "your network"
    return str(ip)


_AGENT_TAIL = re.compile(r" · (?P<ua>(?:Mozilla|curl|python|Go-http|okhttp|Dart)[^\n]*)$")
_FROM = re.compile(r" from (?P<ip>[0-9a-fA-F.:]+(?: via [0-9a-fA-F.:]+)?)(?= at\b|$)")


def humane(text: str) -> str:
    """An alert as it was logged before it said this in words: the trailing user-agent turned into a browser and the address into a
    place. A text with neither comes back as it was."""
    agent = _AGENT_TAIL.search(text)
    if agent:
        name = browser(agent.group("ua"))
        text = text[: agent.start()] + (f" · {name}" if name else "")
    return _FROM.sub(lambda m: f" from {where(m.group('ip')) or m.group('ip')}", text)

"""Keeps the selected car on every in-site link of a page. A link that carries no ?vin= sends the
sidebar to the first car (or off All cars) on the next page; the templates carry it with {{ q }}
where they remember to, and this closes the rest in one place, on the rendered page."""

from __future__ import annotations

import re

# The pages a link may carry the car to: the start page, every Settings page, and the top-level
# sections. A link to one drive, day or place keeps its own car, and files and the API are not pages.
SECTIONS = ("battery", "calendar", "charges", "charging", "data", "day", "drives", "map", "parked", "places", "service", "sleep", "software", "stats", "tags", "timeline")

_HREF = re.compile(r'(<a\b[^>]*?\shref=")(?P<root>{root})(?P<path>/[^"?#]*)(?P<query>\?[^"#]*)?(?P<frag>#[^"]*)?"')


def wants_car(path: str) -> bool:
    path = path.rstrip("/") or "/"
    return path == "/" or path == "/settings" or path.startswith("/settings/") or path.removeprefix("/") in SECTIONS


def keep_car_links(html: str, root: str, vin: str) -> str:
    """`html` with `vin` added to every in-site page link that has none."""
    if not vin:
        return html
    pattern = re.compile(_HREF.pattern.replace("{root}", re.escape(root)))

    def add(m: re.Match) -> str:
        query = m.group("query") or ""
        if not wants_car(m.group("path")) or "vin=" in query:
            return m.group(0)
        sep = "&amp;" if query else "?"
        return f'{m.group(1)}{m.group("root")}{m.group("path")}{query}{sep}vin={vin}{m.group("frag") or ""}"'

    return pattern.sub(add, html)


def redirect_with_car(location: str, root: str, vin: str | None) -> str:
    """A redirect to one of this site's pages that names no car, with the one the request was
    about. Anything else (another host, a file, the API, the start page) is returned as it was."""
    if not vin or not location.startswith(root + "/") or location.startswith(root + "//"):
        return location
    path, hash_, frag = location[len(root) :].partition("#")
    page, _, query = path.partition("?")
    if page == "/" or not wants_car(page) or "vin=" in query:
        return location
    return f"{root}{page}?{query + '&' if query else ''}vin={vin}{hash_}{frag}"

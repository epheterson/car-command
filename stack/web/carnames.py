"""A car's name as the site shows it: the owner's rename when there is one, else the source's.

The rename is a display override kept in settings (`car_names`, vin -> name); the source's own name is never
touched. Two cars that would show the same name are told apart by the last digits of their VINs. Pure.
"""

from __future__ import annotations

MAX_LEN = 40


def clean(text) -> str:
    """A typed name: one line, single spaces, at most MAX_LEN characters; blank when not text."""
    return " ".join(text.split())[:MAX_LEN] if isinstance(text, str) else ""


def display_names(sources: dict[str, str], overrides: dict | None) -> dict[str, str]:
    """vin -> shown name, for every car in `sources` (vin -> the source's name)."""
    overrides = overrides if isinstance(overrides, dict) else {}
    base = {vin: clean(overrides.get(vin)) or name or vin for vin, name in sources.items()}
    groups: dict[str, list[str]] = {}
    for vin, name in base.items():
        groups.setdefault(name.casefold(), []).append(vin)
    out = dict(base)
    for vins in groups.values():
        if len(vins) < 2:
            continue
        for n in range(4, max(len(v) for v in vins) + 1):
            tails = {v: v[-n:] for v in vins}
            if len(set(tails.values())) == len(vins):
                break
        for vin in vins:
            out[vin] = f"{base[vin]} ({tails[vin]})"
    return out

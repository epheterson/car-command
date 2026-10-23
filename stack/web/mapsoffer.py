"""The one-time offer of map backgrounds and place names, made after the first history comes in.

Both switches start off, so no position leaves the house unasked. This is the same two switches as Settings › Maps,
put to the owner once, on the pages whose maps are empty (templates/_mapsoffer.html); either answer is written down and it is not asked again."""

from __future__ import annotations

KEY = "maps_offer"


def answer(choice: str | None) -> dict | None:
    """The settings an answer writes, or None for anything but the two answers the offer's buttons send."""
    if choice == "on":
        return {"public_tiles": True, "public_geocoder": True, KEY: "on"}
    if choice == "no":
        return {KEY: "no"}
    return None

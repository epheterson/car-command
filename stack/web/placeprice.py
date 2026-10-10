"""What charging costs at a named place, as the owner set it on Places or in the picker.

Stored on the place (cc_places.price, JSONB), NULL when not set:
  {"kind": "free"}                         nothing to pay
  {"kind": "home"}                         the home plan, by the hour power was drawn
  {"kind": "average"}                      the regional household average, an estimate
  {"kind": "kwh", "rate": 0.30, "fee": 1}  a flat price per kWh, plus an optional per-session fee
  {"kind": "minute", "rate": 0.25}         a price per minute plugged in, plus the same fee

Nothing is assumed from a name: a place called Work is not free until the owner says so.
A place named Home with no price is priced on the home plan, because the home plan is
what prices home charges anyway (facts.price_plan); setting one here overrides that.
Pure; the routes bring the form."""

from __future__ import annotations

KINDS = {"": "Not set", "free": "Free", "home": "Home plan", "kwh": "Per kWh", "minute": "Per minute", "average": "Regional average"}
PAID = ("kwh", "minute")
MAX_RATE = 5.0  # per kWh or per minute; anything above is a typo, as on Settings › Money
MAX_FEE = 100.0


class BadPrice(ValueError):
    """A paid price without a usable rate, or a kind this module does not know."""


def _number(text, lo: float, hi: float, what: str) -> float | None:
    text = str(text if text is not None else "").strip()
    if not text:
        return None
    try:
        v = float(text)
    except ValueError:
        raise BadPrice(f"{what} is not a number.") from None
    if not lo <= v <= hi:
        raise BadPrice(f"{what} must be between {lo:g} and {hi:g}.")
    return v


def clean(kind, rate=None, fee=None) -> dict | None:
    """The stored price for a posted kind, rate and fee; None for not set. A paid kind
    needs a rate above zero; the fee is optional and only kept when above zero."""
    kind = (kind or "").strip()
    if kind not in KINDS:
        raise BadPrice("Not a kind of price.")
    if not kind:
        return None
    if kind not in PAID:
        return {"kind": kind}
    r = _number(rate, 0.0, MAX_RATE, "The rate")
    if not r:
        raise BadPrice("A paid place needs its price per kWh or per minute.")
    f = _number(fee, 0.0, MAX_FEE, "The session fee")
    return {"kind": kind, "rate": r, **({"fee": f} if f else {})}


def from_form(form) -> tuple[bool, dict | None]:
    """(whether the form carried a price at all, the price). A form without the field
    (an older page, a rename from elsewhere) leaves the stored price alone."""
    if "price_kind" not in form:
        return False, None
    kind = form.get("price_kind")
    if kind == "paid":  # the line's Paid… choice; price_per says per kWh or per minute
        kind = form.get("price_per") if form.get("price_per") in PAID else "kwh"
    return True, clean(kind, form.get("price_rate"), form.get("price_fee"))


def valid(price) -> bool:
    """Whether a stored price has the shape pricing reads; anything else prices nothing."""
    try:
        return price is None or clean(price.get("kind"), price.get("rate"), price.get("fee")) == price
    except (BadPrice, AttributeError):
        return False


def describe(price: dict | None, home: bool = False, currency: str = "$") -> str:
    """One short phrase: "Free", "Home plan", "$0.30/kWh + $1.00 a session"."""
    if not price:
        return "Home plan" if home else "Not set"
    kind = price.get("kind")
    if kind in PAID:
        unit = "kWh" if kind == "kwh" else "min"
        fee = f" + {currency}{price['fee']:.2f} a session" if price.get("fee") else ""
        return f"{currency}{price['rate']:.2f}/{unit}{fee}"
    return KINDS.get(kind, "Not set")


def cost(price: dict, kwh: float, minutes: float) -> float:
    """What a paid price comes to for a charge of `kwh` over `minutes`."""
    per = kwh if price["kind"] == "kwh" else minutes
    return per * float(price["rate"]) + float(price.get("fee") or 0.0)


def label(price: dict, place: str) -> str:
    """Stored with the cost (cc_charge_facts.plan): what priced it, where."""
    if price["kind"] == "free":
        return f"Free at {place}"
    unit = "kWh" if price["kind"] == "kwh" else "min"
    fee = f" + {float(price['fee']):.2f}/session" if price.get("fee") else ""
    return f"{place} at {float(price['rate']):.2f}/{unit}{fee}"

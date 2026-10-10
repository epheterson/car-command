"""The other brands: the CarConnectivity connectors on PyPI that Settings › Cars and sources
can add, with the settings each release reads (checked against the connector's own code on
2026-10-04). Which package, release, connector type and id each is, and the sha256 of every
wheel it installs, come from connectors.json: the same file the connector container ships
and enforces (stack/shared/connectors.json, written by scripts/connector-lock.py). Pure:
nothing here touches the network, the database or pip.

Left out on purpose: Volvo (carconnectivity-connector-volvo 0.1a2 pins requests~=2.32.5,
which the image's requests 2.34 rules out, so it cannot install beside what runs), and
Renault/Dacia (0.1a1, a single alpha release not in CarConnectivity's own list). Lucid
has its own source; Tesla comes in through Connect Tesla, TeslaFi or TeslaMate."""

from __future__ import annotations

import json
import re
from pathlib import Path

LOCK: dict = json.loads((Path(__file__).with_name("connectors.json")).read_text(encoding="utf-8"))["connectors"]
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Field:
    key: str  # the connector config key it fills
    label: str
    kind: str = "text"  # text, email, password, select, vins
    required: bool = True
    options: tuple[tuple[str, str], ...] = ()  # (value, label) for a select
    placeholder: str = ""
    pattern: str = ""  # a whole-value regular expression, when the connector expects a shape

    @property
    def secret(self) -> bool:
        return self.kind == "password"


@dataclass(frozen=True)
class Connector:
    key: str  # unique here; the connector_id when two entries share a type
    name: str  # the choice as the page shows it
    makes: tuple[str, ...]  # the manufacturer names its cars report, for matching cars to the account; () for any
    fields: tuple[Field, ...]
    docs: str
    fixed: dict = field(default_factory=dict)  # config values the choice implies
    note: str = ""

    @property
    def pinned(self) -> dict:
        return LOCK[self.key]

    @property
    def package(self) -> str:
        return self.pinned["package"]

    @property
    def version(self) -> str:
        return self.pinned["version"]

    @property
    def type(self) -> str:  # the CarConnectivity connector type
        return self.pinned["type"]

    @property
    def spec(self) -> str:
        return f"{self.package}=={self.version}"


EMAIL = Field("username", "E-mail", "email")
PASSWORD = Field("password", "Password", "password")
SPIN = Field("spin", "S-PIN (optional, for remote commands)", "password", required=False, pattern=r"\d{4}")

VW_GROUP_MAKES = ("Volkswagen", "VW", "Audi", "Škoda", "Skoda", "SEAT", "Seat", "Cupra", "CUPRA", "Bentley")

CATALOGUE: tuple[Connector, ...] = (
    Connector(
        "audi",
        "Audi",
        ("Audi",),
        (EMAIL, PASSWORD, SPIN),
        "https://github.com/acfischer42/CarConnectivity-connector-audi",
        note="The myAudi account. A community connector (acfischer42).",
    ),
    Connector(
        "cupra",
        "Cupra",
        ("Cupra", "CUPRA"),
        (EMAIL, PASSWORD, SPIN),
        "https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra",
        fixed={"brand": "cupra"},
        note="The My CUPRA app's account.",
    ),
    Connector(
        "seat",
        "SEAT",
        ("SEAT", "Seat"),
        (EMAIL, PASSWORD, SPIN),
        "https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra",
        fixed={"brand": "seat"},
        note="The My SEAT app's account.",
    ),
    Connector(
        "skoda",
        "Škoda",
        ("Škoda", "Skoda", "SKODA"),
        (
            Field("api_key", "API key", "password"),
            Field("vins", "VINs", "vins", placeholder="one or more, separated by commas"),
        ),
        "https://github.com/tillsteinbach/CarConnectivity-connector-skoda",
        note="Škoda's official public API: make a key in the MyŠkoda app (8.16 or newer), and name each car by its VIN; the API cannot list them. It allows 20 requests an hour, so readings come every five minutes.",
    ),
    Connector(
        "tronity",
        "Tronity",
        (),
        (Field("client_id", "Client ID"), Field("client_secret", "Client secret", "password")),
        "https://github.com/tillsteinbach/CarConnectivity-connector-tronity",
        note="Tronity (tronity.io) reads many brands; its platform app's client ID and secret.",
    ),
    Connector(
        "volkswagen",
        "Volkswagen (Europe)",
        ("Volkswagen", "VW"),
        (EMAIL, PASSWORD, SPIN),
        "https://github.com/tillsteinbach/CarConnectivity-connector-volkswagen",
        note="The Volkswagen app's account. Volkswagen changed its sign-in in May 2026 and this release may not get through it yet; the EU Data Act choice is the fallback.",
    ),
    Connector(
        "volkswagen_na",
        "Volkswagen (US and Canada)",
        ("Volkswagen", "VW"),
        (EMAIL, PASSWORD, SPIN, Field("country", "Country", "select", options=(("us", "United States"), ("ca", "Canada")))),
        "https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na",
        note="The myVW app's account. A community connector (zackcornelius); Canada is untested upstream.",
    ),
    Connector(
        "vw_eu_data_act",
        "VW Group (EU Data Act)",
        VW_GROUP_MAKES,
        (
            EMAIL,
            PASSWORD,
            Field(
                "brand",
                "Make",
                "select",
                options=(
                    ("VOLKSWAGEN_PASSENGER_CARS", "Volkswagen"),
                    ("VOLKSWAGEN_COMMERCIAL_VEHICLES", "Volkswagen Commercial Vehicles"),
                    ("AUDI", "Audi"),
                    ("SKODA", "Škoda"),
                    ("SEAT", "SEAT"),
                    ("CUPRA", "Cupra"),
                    ("BENTLEY", "Bentley"),
                ),
            ),
        ),
        "https://github.com/mikrohard/CarConnectivity-connector-vw-eu-data-act",
        note="Read-only, every 15 minutes, no position. Turn on a continuous 15-minute data request on the EU Data Act portal first. A community connector (mikrohard).",
    ),
)

BY_KEY = {c.key: c for c in CATALOGUE}
VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def get(key: str | None) -> Connector | None:
    return BY_KEY.get((key or "").strip())


def values(c: Connector, form) -> tuple[dict | None, str | None]:
    """The connector's config from the posted form, or why not. Every value is checked against
    what the connector reads; nothing posted that the entry does not name is kept."""
    out: dict = {}
    for f in c.fields:
        raw = form.get(f.key)
        raw = raw if isinstance(raw, str) else ""
        text = raw if f.secret else raw.strip()
        if not text:
            if f.required:
                return None, f"{c.name} needs its {f.label.split(' (')[0].lower()}."
            continue
        if len(text) > 500:
            return None, f"That {f.label.lower()} is too long."
        if f.kind == "select" and text not in {v for v, _ in f.options}:
            return None, f"Pick a {f.label.lower()} from the list."
        if f.kind == "email" and "@" not in text:
            return None, "That is not an e-mail address."
        if f.pattern and not re.fullmatch(f.pattern, text):
            return None, f"The {f.label.split(' (')[0]} is four digits." if f.key == "spin" else f"That {f.label.lower()} does not look right."
        if f.kind == "vins":
            vins = [v.strip().upper() for v in re.split(r"[\s,;]+", text) if v.strip()]
            if not vins or not all(VIN.fullmatch(v) for v in vins):
                return None, "A VIN is 17 letters and digits; separate several with commas."
            out[f.key] = vins
            continue
        out[f.key] = text
    out.update(c.fixed)
    return out, None


def row_config(c: Connector) -> dict:
    """What the connector container needs besides the sealed values, kept in plain sight:
    which package at which release, which connector type, and the state of the request."""
    cfg = {"connector": c.key, "brand": c.name, "makes": list(c.makes), "package": c.package, "version": c.version, "type": c.type, "state": "install"}
    if c.pinned.get("connector_id"):
        cfg["connector_id"] = c.pinned["connector_id"]  # two entries share a type: CarConnectivity needs the ids to differ
    return cfg


def claims(config: dict, make: str | None) -> bool:
    """Whether a car of this make is one this account answers for."""
    makes = [m.lower() for m in (config.get("makes") or [])]
    m = (make or "").strip().lower()
    if not makes:
        return bool(m) and m != "lucid"  # a connector that reads many brands: any car the connector has that is not Lucid's
    return m in makes

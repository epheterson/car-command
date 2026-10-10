"""The other brands' catalogue: every entry installable, every form checked, nothing kept that was not asked."""

import importlib.util
import json
import re
from pathlib import Path

import connectors
from connectors import CATALOGUE, claims, get, row_config, values

# The connector container's own gate (stack/cc/plugins.py): every entry here must pass it.
_spec = importlib.util.spec_from_file_location("cc_plugins", Path(__file__).parent.parent / "cc" / "plugins.py")
cc_plugins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc_plugins)


def test_every_entry_is_well_formed_and_passes_the_connector_gate():
    assert len({c.key for c in CATALOGUE}) == len(CATALOGUE) and len({c.name for c in CATALOGUE}) == len(CATALOGUE)
    for c in CATALOGUE:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", c.key), c.key
        assert cc_plugins.problem(row_config(c)) is None, c.key
        assert c.spec == f"{c.package}=={c.version}" and "==" in c.spec and not re.search(r"[<>~*]", c.version)
        assert c.docs.startswith("https://github.com/") and c.note
        keys = [f.key for f in c.fields]
        assert len(keys) == len(set(keys)) and not {"kind", "connector", "csrf_token"} & set(keys), "form names that collide with the page's own"
        assert any(f.secret for f in c.fields), "every brand signs in with something secret"
        for f in c.fields:
            assert f.kind in {"text", "email", "password", "select", "vins"}
            assert (f.kind == "select") == bool(f.options)
            if f.key in ("password", "spin", "api_key", "client_secret"):
                assert f.secret, f"{c.key}.{f.key} is a password input"
    assert get("cupra").type == get("seat").type == "seatcupra"
    assert row_config(get("cupra"))["connector_id"] == "cupra" and "connector_id" not in row_config(get("audi")), "an id only where two entries share a type"


def test_the_brands_on_offer_are_the_ones_verified():
    assert [c.key for c in CATALOGUE] == ["audi", "cupra", "seat", "skoda", "tronity", "volkswagen", "volkswagen_na", "vw_eu_data_act"]
    assert not any(c.type in ("volvo", "renaultdacia", "lucid") for c in CATALOGUE)
    assert {c.package for c in CATALOGUE} == {
        "carconnectivity-connector-audi",
        "carconnectivity-connector-seatcupra",
        "carconnectivity-connector-skoda",
        "carconnectivity-connector-tronity",
        "carconnectivity-connector-volkswagen",
        "carconnectivity-connector-volkswagen-na",
        "carconnectivity-connector-vw-eu-data-act",
    }


def test_values_keep_only_what_the_connector_reads():
    vw = get("volkswagen")
    got, err = values(vw, {"username": " owner@example.com ", "password": " pw with spaces ", "spin": "", "evil": "x"})
    assert err is None and got == {"username": "owner@example.com", "password": " pw with spaces "}, "passwords kept exactly; blanks and strays dropped"
    assert values(vw, {"username": "owner@example.com"})[1] == "Volkswagen (Europe) needs its password."
    assert "e-mail" in values(vw, {"username": "owner", "password": "x"})[1]
    assert "four digits" in values(vw, {"username": "o@example.com", "password": "x", "spin": "12a4"})[1]
    got, _ = values(get("cupra"), {"username": "o@example.com", "password": "x", "spin": "1234", "brand": "seat"})
    assert got["brand"] == "cupra" and got["spin"] == "1234", "the choice fixes the brand, whatever was posted"
    assert "from the list" in values(get("volkswagen_na"), {"username": "o@example.com", "password": "x", "country": "de"})[1]
    got, _ = values(get("skoda"), {"api_key": "k-123", "vins": "alpha000000000001, CHARL000000000002"})
    assert got["vins"] == ["ALPHA000000000001", "CHARL000000000002"]
    assert "17 letters" in values(get("skoda"), {"api_key": "k", "vins": "short"})[1]
    assert "too long" in values(vw, {"username": "o@example.com", "password": "x" * 501})[1]


def test_an_error_never_repeats_what_was_typed():
    for c in CATALOGUE:
        form = {f.key: "hunter2-secret" for f in c.fields}
        _got, err = values(c, form)
        assert err is None or "hunter2" not in err


def test_cars_are_matched_to_an_account_by_their_make():
    assert claims(row_config(get("volkswagen")), "Volkswagen") and not claims(row_config(get("volkswagen")), "Audi")
    assert claims(row_config(get("skoda")), "ŠKODA".title()) or claims(row_config(get("skoda")), "Skoda")
    tronity = row_config(get("tronity"))
    assert claims(tronity, "BMW") and not claims(tronity, "Lucid") and not claims(tronity, None), "a many-brand service: any make but Lucid's"
    assert json.loads(json.dumps(row_config(get("audi")))) == row_config(get("audi")), "the row's config is plain JSON"
    assert connectors.get(None) is None and connectors.get("volvo") is None


def test_the_site_ships_the_shared_catalogue_and_offers_exactly_its_entries():
    here = Path(__file__).parent
    assert (here / "connectors.json").read_bytes() == (here.parent / "shared" / "connectors.json").read_bytes(), "run make connector-lock"
    assert [c.key for c in CATALOGUE] == list(connectors.LOCK), "every pinned entry has a form, and every form a pinned entry"
    for c in CATALOGUE:
        assert c.pinned["requirements"][0]["name"] == c.package and c.pinned["requirements"][0]["version"] == c.version
    assert "connectors.json" in (here / "Dockerfile").read_text()

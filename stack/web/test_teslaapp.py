"""The Tesla app's entered values: pure."""

import pytest
from cryptography.fernet import Fernet

import sources
import teslaapp


def test_clean_domain_keeps_the_hostname_alone():
    assert teslaapp.clean_domain("https://Cars.Example.test/carcommand?x=1") == "cars.example.test"
    assert teslaapp.clean_domain("cars.example.test") == "cars.example.test"
    for bad in ("", "localhost", "192.168.1.5", "has space.example", "http://"):
        assert teslaapp.clean_domain(bad) == "", bad


def test_env_wins_field_by_field_and_says_where_each_came_from():
    got = teslaapp.merge({"client_id": "client-alpha"}, {"client_id": "client-bravo", "client_secret": "s", "domain": "a.example.test"})
    assert got["client_id"] == "client-alpha" and got["client_secret"] == "s"
    assert got["from"] == {"client_id": "env", "client_secret": "app", "domain": "app"}
    assert teslaapp.merge({}, {})["from"] == {"client_id": "", "client_secret": "", "domain": ""}


def test_a_blank_field_keeps_what_is_kept_and_an_env_field_is_never_stored():
    kept = {"client_id": "client-alpha", "client_secret": "s", "domain": "a.example.test"}
    values, why = teslaapp.read_form({"client_id": "", "client_secret": "", "domain": "b.example.test"}, kept, {})
    assert why is None and values == {"client_id": "client-alpha", "client_secret": "s", "domain": "b.example.test"}
    values, why = teslaapp.read_form({"client_id": "x", "client_secret": "t", "domain": "c.example.test"}, {}, {"client_id": "client-bravo"})
    assert why is None and "client_id" not in values
    _, why = teslaapp.read_form({"client_id": "x"}, {}, {})
    assert why == "Still needed: Client secret, Domain."


def test_the_values_are_sealed_for_their_own_place_and_read_back():
    vault = sources.Vault(Fernet.generate_key().decode())
    blob = teslaapp.pack(vault, {"client_id": "client-alpha", "client_secret": "s", "domain": "a.example.test"})
    assert "client-alpha" not in blob
    assert teslaapp.unpack(vault, blob)["client_secret"] == "s"
    assert teslaapp.unpack(sources.Vault(Fernet.generate_key().decode()), blob) == {}, "another install's key opens nothing"
    assert teslaapp.unpack(vault, None) == {}
    with pytest.raises(sources.NoKey):
        teslaapp.pack(sources.Vault(None), {})

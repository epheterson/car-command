"""The app's key: made once, sealed, and its public half served where Tesla reads it."""

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import sources
import tesla_keys
from test_empty import bare, client  # noqa: F401 - fixtures

PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"


@pytest.fixture
def vault():
    return sources.Vault(Fernet.generate_key().decode())


class Store(dict):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def set(self, key, value):
        self.writes += 1
        self[key] = value


def test_the_key_is_p256_and_its_public_half_round_trips():
    pem = tesla_keys.new_private_pem()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(key.curve, ec.SECP256R1)
    pub = serialization.load_pem_public_key(tesla_keys.public_pem(pem).encode())
    assert pub.public_numbers() == key.public_key().public_numbers()


def test_the_hex_form_is_the_uncompressed_point_tesla_hands_back():
    h = tesla_keys.public_hex(tesla_keys.new_private_pem())
    assert h.startswith("04") and len(h) == 130 and int(h, 16)


def test_ensure_makes_the_key_once_and_seals_it(vault):
    store = Store()
    first = tesla_keys.ensure(store.get, store.set, vault)
    second = tesla_keys.ensure(store.get, store.set, vault)
    assert first == second and store.writes == 1
    assert "PRIVATE KEY" not in store[tesla_keys.STATE_KEY], "kept sealed, never as plain PEM"
    assert tesla_keys.stored(store.get, vault) == first


def test_without_a_vault_key_nothing_is_made_and_nothing_reads():
    store, empty = Store(), sources.Vault(None)
    with pytest.raises(sources.NoKey):
        tesla_keys.ensure(store.get, store.set, empty)
    assert store.writes == 0 and tesla_keys.stored(store.get, empty) is None


def test_the_public_path_answers_404_until_there_is_a_key_and_never_makes_one(bare, client, monkeypatch, vault):  # noqa: F811
    store = Store()
    monkeypatch.setattr(bare, "VAULT", vault)
    monkeypatch.setattr(bare, "state_get", store.get)
    monkeypatch.setattr(bare, "state_set", store.set)
    assert client.get(PATH).status_code == 404
    assert store.writes == 0
    pem = tesla_keys.ensure(store.get, store.set, vault)
    r = client.get(PATH)
    assert r.status_code == 200 and r.text == tesla_keys.public_pem(pem)
    assert r.headers["content-type"].startswith("application/x-pem-file")

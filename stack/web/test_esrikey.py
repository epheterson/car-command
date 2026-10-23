"""The ArcGIS key the satellite tiles need: checked, sealed like the OpenEI key, shown back as its last four."""

import json

import pytest

import esrikey


class Conn:
    def __init__(self):
        self.rows = {}

    def execute(self, sql, args=()):
        if sql.startswith("INSERT"):
            self.rows[args[0]] = args[1]
        elif sql.startswith("DELETE"):
            self.rows.pop(args[0], None)
        row = {"value": self.rows[args[0]]} if sql.startswith("SELECT") and args[0] in self.rows else None
        return type("R", (), {"fetchone": lambda self_: row})()


class Vault:
    def seal(self, text, kind, sid):
        return text[::-1].encode()

    def open(self, blob, kind, sid, legacy=False):
        return blob.decode()[::-1]


def test_a_key_is_one_long_run_of_letters_and_digits():
    good = "AAPK" + "a1B2" * 12
    assert esrikey.check_key(f"  {good}  ") == good
    for bad in ("", "short", "has space " * 5, "<script>" + "a" * 30, "a" * 401):
        with pytest.raises(esrikey.EsriKeyError):
            esrikey.check_key(bad)


def test_a_saved_key_is_sealed_shown_as_its_last_four_and_opens_again():
    conn, vault, key = Conn(), Vault(), "AAPK" + "x9Y8" * 12
    assert esrikey.key_hint(conn) is None and esrikey.load_key(conn, vault) is None
    esrikey.save_key(conn, vault, key)
    stored = json.loads(conn.rows[esrikey.KEY_STATE])
    assert key not in conn.rows[esrikey.KEY_STATE], "the plain key is never stored"
    assert esrikey.key_hint(conn) == stored["last4"] and key.endswith(stored["last4"].lstrip("…"))
    assert esrikey.load_key(conn, vault) == key
    esrikey.forget_key(conn)
    assert esrikey.key_hint(conn) is None

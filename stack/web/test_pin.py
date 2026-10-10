from pathlib import Path

import pytest

from app import pin_place


class Conn:
    """Enough of a psycopg connection for pin_place: cc_places in a list."""

    def __init__(self, rows):
        self.rows = rows
        self.next_id = 100
        self.state = {}

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.endswith("WHERE merged_into = %s"):
            return Res([])
        if s.startswith("SELECT value FROM cc_state"):
            return Res([{"value": self.state[args[0]]}] if args[0] in self.state else [])
        if s.startswith("INSERT INTO cc_state"):
            self.state[args[0]] = args[1]
            return Res([])
        if s.startswith("SELECT id, name, latitude, longitude, radius_m") and "FROM cc_places" in s:
            return Res(self.rows)
        if s.startswith("UPDATE cc_places SET name = %s, auto = FALSE, icon = %s WHERE id = %s"):
            name, icon, pid = args
            for r in self.rows:
                if r["id"] == pid:
                    r["name"], r["auto"], r["icon"] = name, False, icon
            return Res([])
        if s.startswith("UPDATE cc_places SET name = %s, auto = FALSE WHERE id = %s"):
            name, pid = args
            for r in self.rows:
                if r["id"] == pid:
                    r["name"], r["auto"] = name, False
            return Res([])
        if s.startswith("INSERT INTO cc_places"):
            name, lat, lon, radius, icon = args
            self.rows.append({"id": self.next_id, "name": name, "latitude": lat, "longitude": lon, "radius_m": radius, "auto": False, "icon": icon})
            return Res([{"id": self.next_id}])
        raise AssertionError(s)

    def commit(self):
        pass


class Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_pinning_inside_an_auto_place_renames_it_and_keeps_its_id():
    conn = Conn([{"id": 7, "name": "Evergreen Terrace, Springfield", "latitude": 39.8010, "longitude": -89.6500, "radius_m": 150, "auto": True}])
    assert pin_place(conn, 39.8011, -89.6501, "Bravo Market") == 7
    assert conn.rows[0]["name"] == "Bravo Market" and conn.rows[0]["auto"] is False and len(conn.rows) == 1


def test_pinning_somewhere_new_adds_a_pinned_place():
    conn = Conn([])
    assert pin_place(conn, 39.8100, -89.6400, "Alpha Office") == 100
    assert conn.rows[0]["auto"] is False and conn.rows[0]["radius_m"] == 120


def test_a_blank_name_is_refused():
    with pytest.raises(ValueError):
        pin_place(Conn([]), 39.81, -89.64, "   ")


def test_pinning_with_an_icon_stores_it_on_insert_and_rename():
    conn = Conn([{"id": 7, "name": "Evergreen Terrace, Springfield", "latitude": 39.8010, "longitude": -89.6500, "radius_m": 150, "auto": True}])
    pin_place(conn, 39.8011, -89.6501, "Bravo Market", icon="shop")
    assert conn.rows[0]["icon"] == "shop"
    conn2 = Conn([])
    pin_place(conn2, 39.81, -89.64, "Alpha Office", icon="work")
    assert conn2.rows[0]["icon"] == "work"


def test_pinning_with_no_icon_stores_none():
    conn = Conn([])
    pin_place(conn, 39.81, -89.64, "Alpha Office")
    assert conn.rows[0]["icon"] is None


def test_pinning_an_existing_place_without_choosing_an_icon_keeps_its_icon():
    """No icon chosen leaves the place's own; a chosen one, or an explicit "none", replaces it."""
    conn = Conn([{"id": 7, "name": "Evergreen Terrace, Springfield", "latitude": 39.8010, "longitude": -89.6500, "radius_m": 150, "auto": True, "icon": "home"}])
    pin_place(conn, 39.8011, -89.6501, "Home")
    assert conn.rows[0]["icon"] == "home" and conn.rows[0]["name"] == "Home" and conn.rows[0]["auto"] is False
    pin_place(conn, 39.8011, -89.6501, "Home", icon="none")
    assert conn.rows[0]["icon"] == "none"
    pin_place(conn, 39.8011, -89.6501, "Home", icon="shop")
    assert conn.rows[0]["icon"] == "shop"


def test_endpoint_names_open_the_naming_card():
    here = Path(__file__).parent
    parts = (here / "templates" / "_parts.html").read_text()
    assert "place_refs(title, refs, true)" in parts and "place_refs(text, refs)" in parts, "row titles and parked lines carry tappable place names"
    shell = (here / "static" / "shell.js").read_text()
    assert '".pn[data-lat]"' in shell and "/static/placecard.js" in shell, "every page opens the card, loading it on the first tap"
    js = (here / "static" / "placecard.js").read_text()
    assert "/places/candidates" in js and "Just the address" not in js, "a blank name saves as the address; no list item for it"

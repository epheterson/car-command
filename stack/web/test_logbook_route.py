"""/logbook.csv: behind the same sign-in as the other exports, filtered by date, car and tag, addresses only on request."""

from test_connect import world  # noqa: F401 - fixture
from test_logbook import drive


def stranger(client):
    return client.__class__(client.app, follow_redirects=False)


def test_a_stranger_is_sent_to_sign_in(world):  # noqa: F811
    app, client = world
    r = stranger(client).get("/logbook.csv")
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_the_dates_car_and_tags_reach_the_query_and_the_file_is_a_csv(world, monkeypatch):  # noqa: F811
    app, client = world
    seen = []
    ds = [drive(2, "Work", 5), drive(1, "Personal", 3)]
    monkeypatch.setattr(app, "drives", lambda vin, limit=50, start=None, end=None: seen.append((vin, limit, start, end)) or ds)
    monkeypatch.setattr(app, "api_vin", lambda vin: vin)
    monkeypatch.setattr(app, "where_words", lambda lat, lon: ("Pine Road, Shelbyville", "Shelbyville, IL"))
    r = client.get("/logbook.csv?vin=V1&from=2026-10-01&to=2026-10-31&tag=Work")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "carcommand-logbook.csv" in r.headers["content-disposition"]
    vin, limit, start, end = seen[0]
    assert (vin, limit, start.date().isoformat(), end.date().isoformat()) == ("V1", None, "2026-10-01", "2026-11-01")
    lines = r.text.strip().split("\r\n")
    assert len(lines) == 2 and "Work" in lines[1] and "Personal" not in r.text
    assert "Elm Street" not in r.text and "address" not in lines[0]


def test_addresses_and_totals_are_off_unless_ticked(world, monkeypatch):  # noqa: F811
    app, client = world
    monkeypatch.setattr(app, "drives", lambda vin, limit=50, start=None, end=None: [drive(1, "Work", 5), drive(2, "Personal", 3)])
    monkeypatch.setattr(app, "api_vin", lambda vin: vin)
    monkeypatch.setattr(app, "where_words", lambda lat, lon: ("Pine Road, Shelbyville", "Shelbyville, IL"))
    plain = client.get("/logbook.csv").text
    assert "Elm Street" not in plain and "Total" not in plain
    full = client.get("/logbook.csv?addresses=on&totals=on&tag=Work&tag=Personal").text
    assert "Elm Street, Springfield" in full and "Total,,,,Work" in full and "Total,,,,All" in full


def test_unnamed_ends_read_as_their_town_and_blank_ones_stay_blank(world, monkeypatch):  # noqa: F811
    app, client = world
    monkeypatch.setattr(app, "drives", lambda vin, limit=50, start=None, end=None: [drive(1, "Work", named=False, start_road=None, start_city=None)])
    monkeypatch.setattr(app, "api_vin", lambda vin: vin)
    monkeypatch.setattr(app, "where_words", lambda lat, lon: ("Pine Road, Shelbyville", "Shelbyville, IL"))
    assert "Shelbyville, IL" in client.get("/logbook.csv").text
    assert "Pine Road, Shelbyville" in client.get("/logbook.csv?addresses=on").text

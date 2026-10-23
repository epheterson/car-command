"""The detail pages' routes on a site with no rows: a number that names nothing is a 404."""

from test_empty import bare, client  # noqa: F401 - fixtures


def test_a_drive_or_charge_number_past_any_row_is_a_404(client):  # noqa: F811
    """An id far past int8 or a row that is not there: the page says not found, never a 500."""
    c = client
    for path in ("/drives/99999999999999999999", "/charges/99999999999999999999", "/drives/2147483648", "/parked/99999999999999999999"):
        assert c.get(path).status_code == 404, path


def test_an_address_no_route_matches_gets_the_page_not_raw_json(client):  # noqa: F811
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"] and "Nothing at this address" in r.text and "<h1>Not found</h1>" in r.text
    assert '"detail"' not in r.text
    api = client.get("/api/no-such-thing")
    assert api.status_code == 404 and api.json() == {"error": "Not Found"}, "the API still answers JSON"
    asset = client.get("/static/no-such.js")
    assert asset.status_code == 404 and "text/plain" in asset.headers["content-type"], "a missing asset is not a whole page"
    drive = client.get("/drives/2147483648")
    assert "<h1>Not found</h1>" in drive.text, "a route's own 404 reads the same"

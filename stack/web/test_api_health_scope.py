import json
import os

for k, v in (("DATABASE_URL", "postgresql://u:p@localhost:1/x"), ("REST_USER", "u"), ("REST_PASSWORD", "p")):
    os.environ.setdefault(k, v)

import app  # noqa: E402


def test_api_health_answers_for_the_one_car_asked_about(monkeypatch):
    """A token for one car must pass ?vin= for it, and then sees only that car's checks."""
    checks = [{"vin": "ALPHA000000000001", "name": "Alpha", "findings": []}, {"vin": "BRAVO000000000002", "name": "Bravo", "findings": []}]
    monkeypatch.setattr(app, "data_checks", lambda: checks)
    one = json.loads(app.api_health("BRAVO000000000002").body)
    assert [c["name"] for c in one] == ["Bravo"]
    assert [c["name"] for c in json.loads(app.api_health(app.GARAGE).body)] == ["Alpha", "Bravo"]
    assert [c["name"] for c in json.loads(app.api_health(None).body)] == ["Alpha", "Bravo"]


def test_the_outer_app_serves_no_docs_of_its_own():
    """/docs, /redoc and /openapi.json fall through to the site, behind sign-in."""
    assert (app.app.docs_url, app.app.redoc_url, app.app.openapi_url) == (None, None, None)

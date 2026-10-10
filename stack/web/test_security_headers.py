"""Every answer carries the security headers, and the content policy admits everything the
pages actually load: the pinned CDN scripts and styles, and the public map backgrounds."""

import importlib
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def app_module():
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    os.environ.setdefault("REST_USER", "u")
    os.environ.setdefault("REST_PASSWORD", "p")
    sys.path.insert(0, str(HERE))
    return importlib.import_module("app")


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    import auth

    app_module.AUTH.update(enabled=True, secret=auth.new_secret(), password=auth.hash_password("a good long password", n=2**10))
    return TestClient(app_module.app, follow_redirects=False)


def policy(app_module) -> dict[str, list[str]]:
    out = {}
    for part in app_module.CSP.split(";"):
        name, *values = part.split()
        out[name] = values
    return out


@pytest.mark.parametrize("path", ["/health", "/login", "/cc/login", "/drives", "/carcommand/stats"])
def test_every_answer_carries_the_headers_even_a_redirect(client, path):
    r = client.get(path)
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'self'" in csp


def test_a_refusal_from_the_guard_carries_them_too(client):
    r = client.post("/places", data={})
    assert r.status_code in (400, 401) and r.headers["x-frame-options"] == "DENY"


def _origin(url: str) -> str:
    m = re.match(r"(https://[^/]+)", url)
    return m.group(1) if m else ""


def test_every_cdn_script_and_stylesheet_is_allowed(app_module):
    p = policy(app_module)
    text = "\n".join(t.read_text() for t in (HERE / "templates").glob("*.html"))
    scripts = re.findall(r'<script[^>]+src="(https://[^"]+)"', text)
    styles = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="(https://[^"]+)"', text)
    assert scripts and styles, "the map pages load their libraries from a CDN"
    for url in scripts:
        assert _origin(url) in p["script-src"], url
    for url in styles:
        assert _origin(url) in p["style-src"], url


def test_the_public_map_backgrounds_are_allowed(app_module):
    """Opt-in in Settings › Maps: OpenFreeMap's vector styles and tiles (fetched by MapLibre,
    in a blob worker) and OpenStreetMap's raster tiles (Leaflet's fallback, as images)."""
    p = policy(app_module)
    js = (HERE / "static" / "maps.js").read_text()
    styles = re.findall(r'"(https://tiles\.openfreemap\.org/[^"]+)"', js)
    raster = re.findall(r'"(https://tile\.openstreetmap\.org/[^"]+)"', js)
    assert styles and raster
    for url in styles:
        assert _origin(url) in p["connect-src"], url
    for url in raster:
        assert _origin(url) in p["img-src"], url
    assert "blob:" in p["worker-src"] and "data:" in p["img-src"], "MapLibre's worker, and the still map drawn as a data: image"

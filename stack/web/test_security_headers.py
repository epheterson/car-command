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


def policy(app_module, tiles: bool = True, imagery: bool = True) -> dict[str, list[str]]:
    """The policy with the map switches on (what the map tests below check), or off."""
    out = {}
    for part in app_module.build_csp(tiles, imagery).split(";"):
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


def test_no_template_loads_a_script_or_stylesheet_from_outside():
    text = "\n".join(t.read_text() for t in (HERE / "templates").glob("*.html"))
    assert not re.findall(r'<script[^>]+src="https?://', text) and not re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="https?://', text)


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


def test_the_satellite_tiles_are_allowed_as_images_only_with_satellite_on(app_module):
    """Opt-in in Settings › Maps: Esri World Imagery tiles, drawn by Leaflet as images."""
    js = (HERE / "static" / "maps.js").read_text()
    urls = re.findall(r'"(https://ibasemaps-api\.arcgis\.com/[^"]+)"', js)
    assert urls and "server.arcgisonline.com" not in js, "the keyed host only: the open one is not licensed"
    on, off = policy(app_module), policy(app_module, imagery=False)
    for url in urls:
        assert _origin(url) in on["img-src"], url
        assert _origin(url) not in on["connect-src"], "tiles are images, not fetches"
        assert _origin(url) not in off["img-src"]


def test_each_public_host_is_allowed_only_while_its_switch_is_on(app_module):
    off = policy(app_module, tiles=False, imagery=False)
    assert "https://tiles.openfreemap.org" not in off["connect-src"] and "https://tile.openstreetmap.org" not in off["img-src"]
    for k in ("img-src", "script-src", "style-src"):
        assert "unpkg" not in off[k] and "jsdelivr" not in off[k], "the map scripts are vendored: no CDN in the policy"
    tiles = policy(app_module, tiles=True, imagery=False)
    assert "https://tiles.openfreemap.org" in tiles["connect-src"] and "https://ibasemaps-api.arcgis.com" not in tiles["img-src"]


def test_applying_the_settings_rebuilds_the_policy_every_answer_carries(app_module):
    d = app_module.settings_mod.DEFAULTS
    was = app_module.ESRI_KEY
    try:
        app_module.ESRI_KEY = "k" * 30
        app_module.apply_settings({**d, "satellite": True})
        assert "https://ibasemaps-api.arcgis.com" in app_module.SECURITY_HEADERS["Content-Security-Policy"]
        app_module.apply_settings({**d, "satellite": False})
        assert "arcgis" not in app_module.SECURITY_HEADERS["Content-Security-Policy"]
        assert app_module.SECURITY_HEADERS["Content-Security-Policy"] == app_module.CSP
    finally:
        app_module.ESRI_KEY = was
        app_module.apply_settings(dict(d))


def test_a_hashed_static_url_is_kept_a_year_and_nothing_else_is(app_module, client):
    year = "public, max-age=31536000, immutable"
    assert client.get(f"/static/style.css?v={app_module.CSS_VERSION}").headers.get("cache-control") == year
    assert client.get(f"/cc/static/view.js?v={app_module.JS_VERSION}").headers.get("cache-control") == year, "under a prefix too"
    for path in ("/static/style.css?v=0ld0ld00", "/static/view.js", "/static/favicon.svg?v=5", f"/static/favicon.svg?v={app_module.JS_VERSION}"):
        assert "immutable" not in client.get(path).headers.get("cache-control", ""), path
    # the script hash covers every script, so a change to any one moves every URL
    js = sorted((HERE / "static").glob("*.js"))
    assert {"view.js", "placecard.js", "placesmap.js"} <= {f.name for f in js}
    import hashlib

    assert app_module.JS_VERSION == hashlib.md5(b"".join(f.read_bytes() for f in js)).hexdigest()[:8]

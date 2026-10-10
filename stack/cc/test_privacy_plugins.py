"""A connector installed at runtime gets the same privacy layer as the image's own.

sitecustomize.py (on PYTHONPATH=/app) runs in every interpreter the container starts,
including CarConnectivity after a plugin lands in the user site. This starts a fresh
interpreter the way the supervisor does, with a stand-in OSM location service and a
stand-in brand connector installed where pip --user puts them, and checks that the
position the connector hands the service leaves rounded, and that with the lookup off
nothing reaches the network at all."""

import json
import os
import site
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import plugins  # noqa: E402

OSM = """
SENT = []

class _Session:
    def get(self, url, **kw):
        SENT.append(url)
        return None

class OSMLocationService:
    def __init__(self):
        self.osm_session = _Session()

    def location_from_lat_lon(self, latitude, longitude, location=None):
        self.osm_session.get(f"https://nominatim.openstreetmap.org/reverse?lat={latitude}&lon={longitude}")
        return (latitude, longitude)
"""

CONNECTOR = """
from carconnectivity_services.location import osm_location_service as osm

def look_up(lat, lon):
    svc = osm.OSMLocationService()
    return svc.location_from_lat_lon(lat, lon), type(svc.osm_session).__name__, osm.SENT
"""

PROBE = """
import json
import carconnectivity_connectors.fakebrand.connector as c
got, session, sent = c.look_up(39.8012345, -89.6543219)
print(json.dumps({"got": got, "session": session, "sent": sent, "file": c.__file__}))
"""


def _put(root: Path) -> None:
    files = {
        "carconnectivity_services/__init__.py": "",
        "carconnectivity_services/location/__init__.py": "",
        "carconnectivity_services/location/osm_location_service.py": OSM,
        "carconnectivity_connectors/fakebrand/__init__.py": "",  # carconnectivity_connectors itself is a namespace package, as upstream
        "carconnectivity_connectors/fakebrand/connector.py": CONNECTOR,
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _run(env) -> dict:
    done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_connector_from_the_plugins_volume_sends_rounded_positions_or_none(tmp_path):
    base = tmp_path / "plugins"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CC_GEOCODER_URL", "PYTHONNOUSERSITE")}
    env["PYTHONUSERBASE"] = str(base)
    user_site_on = site.ENABLE_USER_SITE is not False and sys.prefix == sys.base_prefix  # a virtualenv turns the user site off
    if user_site_on:
        _put(Path(plugins.user_site(env)))
        env["PYTHONPATH"] = str(HERE)
    else:
        # In a virtualenv the stand-ins go on the path after /app instead. The container's
        # interpreter is not one, and CI runs this with the user site on.
        _put(tmp_path / "extra")
        env["PYTHONPATH"] = os.pathsep.join([str(HERE), str(tmp_path / "extra")])
    # privacy.py needs requests; where this interpreter has it in a user site, the PYTHONUSERBASE
    # above hides it, so it goes on the path, last.
    import requests

    env["PYTHONPATH"] += os.pathsep + os.path.dirname(os.path.dirname(requests.__file__))

    public = _run(env)  # CC_GEOCODER_URL unset: the site chose the public service
    assert public["got"] == [39.801, -89.654], "rounded to about 100 m before it leaves"
    assert public["sent"] == ["https://nominatim.openstreetmap.org/reverse?lat=39.801&lon=-89.654"]
    if user_site_on:
        assert public["file"].startswith(str(base)), "the connector came from the plugins volume"

    off = _run({**env, "CC_GEOCODER_URL": ""})  # no decision yet, or off
    assert off["session"] == "_Redirect" and off["sent"] == [], "with the lookup off, nothing reaches the network"

    local = _run({**env, "CC_GEOCODER_URL": "http://maps:8090/"})
    assert local["sent"] == ["http://maps:8090/reverse?lat=39.801&lon=-89.654"], "the local maps add-on, rounded too"

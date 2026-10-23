"""The marker that follows the chart along a drive: it glides between two points close in
time and jumps across a gap in the log (maps.js, CCMap.follow, run in node)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).parent / "static" / "maps.js").read_text()

# just enough of a browser and of Leaflet for follow(): a marker that remembers where it was put
HARNESS = """
var document = { documentElement: { getAttribute: function () { return "light"; } }, body: { dataset: {} } };
function getComputedStyle() { return { getPropertyValue: function () { return ""; } }; }
var window = this;
var placed = null;
var L = { circleMarker: function () { return { setLatLng: function (p) { placed = p; return this; }, addTo: function () { return this; }, remove: function () {} }; }, Layer: { extend: function () { return function () {}; } } };
"""


def where(pts, times) -> list:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = HARNESS + JS + f"""
var map = {{ hasLayer: function () {{ return true; }} }};
var mk = CCMap.follow(map, {json.dumps(pts)}, null, "#000");
console.log(JSON.stringify({json.dumps(times)}.map(function (t) {{ mk.at(t); return placed; }})));
"""
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_between_two_close_points_the_marker_glides():
    got = where([[39.80, -89.65, 0], [39.81, -89.64, 60]], [30, 45])
    assert [round(v, 4) for v in got[0]] == [39.805, -89.645] and [round(v, 4) for v in got[1]] == [39.8075, -89.6425]


def test_across_a_gap_in_the_log_it_jumps_and_it_waits_at_either_end():
    pts = [[39.80, -89.65, 0], [39.81, -89.64, 600]]  # ten minutes with nothing logged
    assert where(pts, [100, 500]) == [[39.80, -89.65], [39.81, -89.64]], "the nearer end, not a line across the gap"
    assert where(pts, [-50, 900]) == [[39.80, -89.65], [39.81, -89.64]]


def test_a_replay_runs_straight_past_a_gap_in_the_log():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    track = (Path(__file__).parent / "static" / "track.js").read_text()
    js = (
        "var document = { documentElement: {} }; function getComputedStyle() { return { getPropertyValue: function () { return ''; } }; } var window = this;\n"
        + track
        + "\nvar pts = [[0, 0, 0], [0, 0, 30], [0, 0, 60], [0, 0, 1260], [0, 0, 1290]];"
        + "\nconsole.log(JSON.stringify([CCTrack.skip(pts, 45, 120), CCTrack.skip(pts, 300, 120), CCTrack.skip(pts, 1270, 120), CCTrack.skip(pts, 2000, 120)]));"
    )
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert got == [45, 1260, 1270, 2000], "between close readings the clock runs on; inside a 20-minute hole it jumps to the far side"

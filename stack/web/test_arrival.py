"""The brand's arrival plays on a fresh load and never holds the page: maps and content draw
at once (a design review found Places a blank box for three seconds on a phone while the mark
played). The animation stays smooth by the page keeping its work light: long lists skip their
off-screen rows, and the moving parts get layers only while it plays."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
BASE = (HERE / "templates" / "base.html").read_text()
HEAD = re.search(r"<script>(/\* a fresh load.*?)</script>", BASE, re.S).group(1)

# A browser's globals, enough for the head script: the arrival on or off, listeners kept so
# a test can fire them, timers run by hand.
HARNESS = """
var listeners = {}, timers = [], classes = [], ran = [];
var document = { referrer: "", documentElement: { classList: { add: function (c) { classes.push(c); } } } };
var location = { origin: "http://x" };
var performance = { getEntriesByType: function () { return [{ type: "navigate" }]; } };
function matchMedia() { return { matches: REDUCED }; }
function addEventListener(k, fn) { (listeners[k] = listeners[k] || []).push(fn); }
function removeEventListener(k, fn) { listeners[k] = (listeners[k] || []).filter(function (f) { return f !== fn; }); }
function fire(k, e) { (listeners[k] || []).slice().forEach(function (f) { f(e || {}); }); }
function setTimeout(fn, ms) { timers.push([fn, ms]); }
var window = this;
"""


def run(steps: str, reduced: bool = False) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (
        HARNESS.replace("REDUCED", "true" if reduced else "false")
        + HEAD
        + steps
        + "\nconsole.log(JSON.stringify({ ran: ran, classes: classes, timers: timers.map(function (t) { return t[1]; }), keys: Object.keys(listeners).filter(function (k) { return listeners[k].length; }) }));"
    )
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_a_fresh_load_plays_the_arrival_and_nothing_waits_for_it():
    got = run('ran.push(typeof ccAfterArrival);')
    assert got["classes"] == ["arrive"] and got["timers"] == [4000] and got["keys"] == [], "no listener holds anything back"
    assert got["ran"] == ["undefined"], "there is no queue for work to wait in"


def test_with_reduced_motion_there_is_no_arrival():
    got = run("", reduced=True)
    assert got["classes"] == [] and got["timers"] == [] and got["keys"] == []


@pytest.mark.parametrize("name", ["now.html", "drive.html", "day.html", "calendar.html", "map.html", "tags.html", "place.html"])
def test_no_map_waits_for_the_arrival(name):
    s = (HERE / "templates" / name).read_text()
    assert "CCMap.base(" in s and "ccAfterArrival" not in s, name


def test_a_places_fields_work_from_the_first_frame():
    s = (HERE / "templates" / "place.html").read_text()
    assert s.index('ask.addEventListener("click"') < s.index("CCMap.base(") and "CCSize.bind(" in s


def test_long_lists_skip_off_screen_rows_and_the_mark_is_promoted_only_while_it_plays():
    css = (HERE / "static" / "style.css").read_text()
    for sel in (".plr", ".row-b"):
        rule = re.search(re.escape(sel) + r" \{ content-visibility:auto; contain-intrinsic-size:auto \d+px; \}", css)
        assert rule, sel
    assert "will-change" not in css.replace("html.arrive:not(.arrived) :is(.mark.towing, .side .brand .nm, .side .brand::after) { will-change:transform; }", ""), "a layer only while the arrival plays"

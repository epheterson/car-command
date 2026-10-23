"""The view in the link: how the Map's centre, zoom and layers and a chart's switched-on lines
are written into a URL query and read back (static/view.js, run in node)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).parent / "static" / "view.js").read_text()


def run(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", f"var window = this;\n{JS}\nconsole.log(JSON.stringify({expr}));"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_centre_and_zoom_are_short_and_read_back():
    got = run("CCView.encodeMap(39.800123456, -89.649987654, 11.26)")
    assert got == {"c": "39.8001,-89.65", "z": "11.3"}
    assert run(f"CCView.decodeMap({json.dumps(got)})") == {"lat": 39.8001, "lon": -89.65, "z": 11.3}


def test_a_view_that_is_not_a_view_is_ignored():
    for bad in ({"c": "91,0", "z": "5"}, {"c": "39.8", "z": "5"}, {"c": "a,b", "z": "5"}, {"c": "39.8,-89.6", "z": "40"}, {"c": "39.8,-89.6"}, {}):
        assert run(f"CCView.decodeMap({json.dumps(bad)})") is None, bad


def test_layers_are_written_only_when_they_differ_from_the_usual():
    assert run('CCView.encodeList(["charges"], ["charges"])') is None
    assert run('CCView.encodeList(["heat", "charges"], ["charges"])') == "charges,heat"
    assert run('CCView.encodeList([], ["charges"])') == "none"
    known = '["charges", "heat", "cells"]'
    assert run(f'CCView.decodeList(undefined, {known}, ["charges"])') == ["charges"]
    assert run(f'CCView.decodeList("none", {known}, ["charges"])') == []
    assert run(f'CCView.decodeList("heat,bogus", {known}, ["charges"])') == ["heat"]


def test_a_charts_lines_round_trip_and_only_changed_charts_are_written():
    state = '{"c1": ["power", "speed"], "c2": ["temp"]}'
    assert run(f'CCView.encodeOn({state}, {{"c1": ["power"], "c2": ["temp"]}})') == "c1:power+speed"
    assert run(f'CCView.encodeOn({state}, {{"c1": ["power", "speed"], "c2": ["temp"]}})') is None
    assert run('CCView.decodeOn("c1:power+speed|c2:temp")') == {"c1": ["power", "speed"], "c2": ["temp"]}
    assert run('CCView.decodeOn("junk")') == {}


def test_the_query_keeps_what_else_it_carries_and_drops_what_is_cleared():
    assert run('CCView.merged("?month=2026-09&l=heat", {c: "1,2", z: "9", l: null})') == "?month=2026-09&c=1%2C2&z=9"
    assert run('CCView.merged("?l=heat", {l: null})') == ""
    assert run('CCView.params("?a=1&b=x")') == {"a": "1", "b": "x"}


def test_a_chart_opens_with_the_lines_the_link_names():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (
        "var window = this; var location = { search: '?ov=c1%3Apower%2Bspeed%7Cc2%3Aother' };\n" + JS + "\n"
        "console.log(JSON.stringify([CCView.chartFrom('c1', ['power', 'speed']), CCView.chartFrom('c1', ['speed']), CCView.chartFrom('c3', ['x'])]));"
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [["power", "speed"], ["speed"], None], "a line the chart does not have is dropped; no entry means the chart's own"


def test_a_hostile_view_is_ignored_not_trusted():
    for bad in ({"c": "1e999,0", "z": "5"}, {"c": "Infinity,0", "z": "5"}, {"c": "NaN,NaN", "z": "5"}, {"c": "39.8,-89.6", "z": "-1"}, {"c": ",", "z": ""}, {"c": "39.8,-89.6,3", "z": "5"}):
        assert run(f"CCView.decodeMap({json.dumps(bad)})") is None, bad


def test_cars_hidden_are_six_character_vin_tails_read_strictly():
    assert run('CCView.tail("5YJ3E1EA0JF000001")') == "000001" and run('CCView.tail("abc")') == "ABC"
    assert run('CCView.encodeIds(["5YJ3E1EA0JF000001", "5YJ3E1EA0JF000001", "LUCIDAIR000042"])') == "000001,000042"
    assert run('CCView.encodeIds([])') is None
    assert run('CCView.decodeIds("000001,000042")') == ["000001", "000042"]
    for hostile in (",0", "0", ",,", "<b>", "000001;x", "000001,000001", "abcdef", "../../x"):
        got = run(f"CCView.decodeIds({json.dumps(hostile)})")
        assert all(len(t) == 6 and t.isalnum() and t.upper() == t for t in got), (hostile, got)
    assert run('CCView.decodeIds(",0")') == [] and run('CCView.decodeIds("000001,000001")') == ["000001"]


def test_a_chart_line_named_twice_in_a_link_is_drawn_once():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = "var window = this; var location = { search: '?ov=c1%3Apower%2Bpower%2Bspeed%2Bpower' };\n" + JS + "\nconsole.log(JSON.stringify(CCView.chartFrom('c1', ['power', 'speed'])));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["power", "speed"]


def test_writes_to_the_address_are_gathered_into_one():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (
        "var window = this, calls = [], timers = [];\n"
        "var history = { state: null, replaceState: function (s, t, u) { calls.push(u); } };\n"
        "var location = { search: '?month=2026-09', pathname: '/map', hash: '' };\n"
        "function setTimeout(f) { timers.push(f); return timers.length; }\n" + JS + "\nfor (var i = 0; i < 50; i++) CCView.set({ c: i + ',1', z: '9' });\n"
        "var before = calls.length; timers.forEach(function (f) { f(); });\n"
        "console.log(JSON.stringify([before, timers.length, calls]));"
    )
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [0, 1, ["/map?month=2026-09&c=49%2C1&z=9"]], "fifty pans, one timer, one write of the last view"

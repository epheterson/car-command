"""The live map's glide: a marker eases from one fix to the next over the time between them,
turns its heading the short way round, and jumps across a gap or when asked for no motion
(maps.js, CCMap.legAt / legMs / turn, run in node)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).parent / "static" / "maps.js").read_text()
HARNESS = """
var document = { documentElement: { getAttribute: function () { return "light"; } }, body: { dataset: {} } };
function getComputedStyle() { return { getPropertyValue: function () { return ""; } }; }
var window = this; var L = { Layer: { extend: function () { return function () {}; } } };
"""


def run(expr: str, pre: str = ""):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", HARNESS + pre + JS + f"\nconsole.log(JSON.stringify({expr}));"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_marker_is_part_way_along_the_leg_by_the_time_that_has_passed():
    leg = "{from: [39.80, -89.65, 0], to: [39.82, -89.63, 90], t0: 1000, ms: 4000}"
    got = run(f"[CCMap.legAt({leg}, 1000), CCMap.legAt({leg}, 3000), CCMap.legAt({leg}, 9000)]")
    assert got[0] == [39.80, -89.65, 0]
    assert [round(v, 4) for v in got[1]] == [39.81, -89.64, 45]
    assert got[2] == [39.82, -89.63, 90], "after the leg it rests on the fix"


def test_the_heading_turns_the_short_way_round_north():
    assert run("[CCMap.turn(350, 10, 0.5), CCMap.turn(10, 350, 0.5), CCMap.turn(90, 180, 0.5)]") == [0, 0, 135]


def test_no_heading_stays_none_and_a_first_heading_is_taken_as_it_is():
    assert run("[CCMap.turn(null, 80, 0.5), CCMap.turn(80, null, 0.5)]") == [80, None]


def test_a_live_glide_takes_the_time_between_fixes_to_six_seconds_and_none_past_fifteen():
    assert run("[CCMap.legMs(2), CCMap.legMs(6), CCMap.legMs(10), CCMap.legMs(15), CCMap.legMs(16), CCMap.legMs(120), CCMap.legMs(0), CCMap.legMs(-3)]") == [2000, 6000, 6000, 6000, 0, 0, 0, 0]


# the glider with a clock and a frame loop of its own: time moves only when the test says so
# the browser's clock and frame loop, global as maps.js finds them: time moves only when the test says so
PRE = """
var clock = 0, frames = [], hidden = false, reduced = false, drawn = [];
var performance = { now: function () { return clock; } };
function requestAnimationFrame(f) { frames.push(f); return frames.length; }
function cancelAnimationFrame(id) { if (id) frames[id - 1] = null; }
Object.defineProperty(document, "hidden", { get: function () { return hidden; } });
window.matchMedia = function () { return { matches: reduced }; };
"""
DRIVER = """
var g = CCMap.glider({ move: function (a, b, h) { drawn.push([+a.toFixed(4), +b.toFixed(4), h === null ? null : Math.round(h)]); } });
function tick(ms) { clock += ms; var f = frames.filter(Boolean); frames = []; f.forEach(function (fn) { fn(clock); }); }
function last() { return drawn[drawn.length - 1]; }
"""


def drive(script: str):
    return run("(function () {" + DRIVER + script + "})()", PRE)


def test_the_first_fix_jumps_and_the_next_glides_from_there():
    got = drive("""
        g.fix(39.80, -89.65, 0);
        var first = [drawn.length, last()];
        clock = 4000; g.fix(39.81, -89.65, 90);
        tick(0); var start = last();
        tick(2000); var mid = last();
        tick(2500); var end = last();
        return [first, start, mid, end];
        """)
    assert got[0] == [1, [39.80, -89.65, 0]], "no earlier fix to glide from"
    assert got[1] == [39.80, -89.65, 0], "the leg starts where the marker was drawn"
    assert got[2] == [39.805, -89.65, 45], "halfway through its four seconds"
    assert got[3] == [39.81, -89.65, 90]


def test_a_new_fix_in_mid_glide_starts_from_where_the_marker_is_not_the_old_fix():
    got = drive("""
        g.fix(39.80, -89.65, 0);
        clock = 4000; g.fix(39.81, -89.65, 0); tick(0); tick(2000);
        var between = last();
        g.fix(39.82, -89.65, 0);
        tick(0);
        return [between, last()];
        """)
    assert got[0] == [39.805, -89.65, 0] and got[1] == [39.805, -89.65, 0], "no snap back to the first fix"


def test_a_long_silence_jumps_and_the_loop_stops_when_the_leg_is_over():
    got = drive("""
        g.fix(39.80, -89.65, 10);
        clock = 40000; g.fix(39.90, -89.60, 20);
        var jumped = [drawn.length, last(), frames.filter(Boolean).length];
        clock = 41000; g.fix(39.91, -89.60, 20);
        tick(0); tick(1000); tick(5000);
        return [jumped, frames.filter(Boolean).length, last()];
        """)
    assert got[0] == [2, [39.90, -89.60, 20], 0], "thirty seconds of nothing is a jump, with no loop left running"
    assert got[1] == 0 and got[2] == [39.91, -89.60, 20], "after the leg, no more frames are asked for"


def test_a_hidden_tab_or_reduced_motion_jumps_and_asks_for_no_frames():
    for flag in ("hidden", "reduced"):
        got = drive(f"g.fix(39.80, -89.65, 0); clock = 3000; {flag} = true; g.fix(39.81, -89.65, 90); return [last(), frames.filter(Boolean).length];")
        assert got == [[39.81, -89.65, 90], 0], flag


def test_a_car_that_starts_moving_turns_from_nothing_and_one_that_stops_loses_its_arrow():
    got = drive("""
        g.fix(39.80, -89.65, null);
        clock = 4000; g.fix(39.81, -89.65, 120); tick(0); tick(4000);
        var moving = last();
        clock = 8000; g.fix(39.81, -89.65, null); tick(0); tick(4000);
        return [moving, last()];
        """)
    assert got == [[39.81, -89.65, 120], [39.81, -89.65, None]]

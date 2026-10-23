"""Maps on a phone: one finger scrolls the page, two move the map, and the note says so once. The
cooperative function in static/maps.js runs here under node against a stand-in map and DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
STATIC = HERE / "static"

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const scenario = JSON.parse(process.argv[3]);
const store = {};
function el() {
  const handlers = {}, kids = [];
  return { handlers, kids, className: "", textContent: "", classList: { add() {}, remove() {} }, setAttribute() {}, appendChild(k) { kids.push(k); }, remove() {},
    addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); } };
}
const box = el();
const map = { dragging: { on: scenario.dragging !== false, enabled() { return this.on; }, disable() { this.on = false; } }, getContainer() { return box; } };
const ctx = {
  console, setTimeout: (f) => 0, Math,
  document: { documentElement: { getAttribute: () => null }, body: { dataset: {} }, createElement: () => el() },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } },
  window: { matchMedia: () => ({ matches: scenario.coarse }) },
};
if (scenario.seen) store["cc-map-hint"] = "1";
ctx.window.window = ctx.window;
Object.assign(ctx, { L: undefined });
vm.createContext(ctx);
vm.runInContext(src, ctx);
const CC = ctx.window.CCMap || ctx.CCMap;
CC.cooperative(map);
const fire = (t, touches) => (box.handlers[t] || []).forEach((f) => f({ touches }));
if (scenario.drag) { fire("touchstart", [{ clientX: 10, clientY: 10 }]); fire("touchmove", [{ clientX: 10, clientY: 60 }]); }
if (scenario.pinch) { fire("touchstart", [{ clientX: 10, clientY: 10 }, { clientX: 50, clientY: 50 }]); fire("touchmove", [{ clientX: 10, clientY: 10 }, { clientX: 90, clientY: 90 }]); }
console.log(JSON.stringify({ dragging: map.dragging.on, hints: box.kids.map((k) => k.textContent), stored: store["cc-map-hint"] || null, listeners: Object.keys(box.handlers) }));
"""


def run(**scenario):
    out = subprocess.run(["node", "-e", HARNESS, "-", str(STATIC / "maps.js"), json.dumps(scenario)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@node
def test_a_phone_map_gives_one_finger_to_the_page_and_says_so_once():
    got = run(coarse=True, drag=True)
    assert got["dragging"] is False and got["hints"] == ["Use two fingers to move the map"] and got["stored"] == "1"
    assert set(got["listeners"]) >= {"touchstart", "touchmove"}


@node
def test_two_fingers_on_the_map_say_nothing_and_a_known_hint_is_not_repeated():
    assert run(coarse=True, pinch=True)["hints"] == []
    assert run(coarse=True, drag=True, seen=True)["hints"] == [], "told once, remembered by this browser"


@node
def test_a_mouse_keeps_dragging_and_a_still_map_is_left_alone():
    got = run(coarse=False, drag=True)
    assert got["dragging"] is True and got["hints"] == [] and got["listeners"] == []
    assert run(coarse=True, dragging=False)["listeners"] == [], "a close-up that never moves has nothing to cooperate with"


def test_every_embedded_map_is_cooperative_and_only_the_big_ones_opt_out():
    maps = (STATIC / "maps.js").read_text()
    assert "if (!opts.full) cooperative(map);" in maps and "cc-map-hint" in (STATIC / "style.css").read_text()
    assert "full: true" in (HERE / "templates" / "map.html").read_text() and "full: true" in (STATIC / "placesmap.js").read_text()
    for page in ("now", "day", "place", "charge", "parked", "calendar", "tags"):
        assert "full: true" not in (HERE / "templates" / f"{page}.html").read_text(), page

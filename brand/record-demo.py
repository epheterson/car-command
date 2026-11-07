"""Record the demo loop, the desktop stills, the banner shots and the phone shot for the social card from a site running the demo car.

    python3 brand/record-demo.py BASE_URL PASSWORD OUT_DIR WORK_DIR [SCREENSHOTS_DIR]

Called by brand/make-demo-media.sh, which brings up a throwaway site first. The loop is one
fresh visit (so the mark's arrival plays on Now), then Timeline, the newest drive and its
map, Stats and Places, each reached from inside the site the way a person would. Playwright
records the phone-sized window; ffmpeg cuts it to the visit and encodes it. With a fifth
argument it also retakes docs/screenshots, the README's six feature shots.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PHONE = {"width": 390, "height": 844}
SCALE = 2  # pages render at twice the CSS size, so stills stay sharp on a high-density screen
FPS = 12
LOOP_WIDTH = 390  # the README shows the loop 260 px wide; a GIF this size stays sharp on a high-density screen at a size GitHub will carry
DESKTOP = {"width": 1280, "height": 800}
# The maps are MapLibre (vector, drawn in the page), not Leaflet tiles: each map built is kept, and
# a page's maps are drawn once every one still on the page has gone idle (its tiles in and painted).
COUNT_MAPS = """(() => {
  let lib;
  Object.defineProperty(window, 'maplibregl', { configurable: true, get: () => lib, set: v => {
    lib = v;
    if (!v || !v.Map || v.Map.__counted) return;
    class Counted extends v.Map {
      constructor(o) { super(o); (window.__maps = window.__maps || []).push(this); this.once('idle', () => { this.__drawn = true; }); }
    }
    Counted.__counted = true;
    v.Map = Counted;
  }});
})();"""
MAPS_DRAWN = (
    "() => { const on = (window.__maps || []).filter(m => m.getCanvas().isConnected); return on.length === document.querySelectorAll('.maplibregl-canvas').length && on.every(m => m.__drawn); }"
)


def context(browser, **kw):
    """A browser context whose pages can tell when their maps are drawn (maps_drawn)."""
    ctx = browser.new_context(**kw)
    ctx.add_init_script(COUNT_MAPS)
    return ctx


def maps_drawn(page) -> None:
    try:
        page.wait_for_function(MAPS_DRAWN, timeout=30000)
    except PlaywrightTimeout:
        seen = page.evaluate("() => [(window.__maps || []).length, document.querySelectorAll('.maplibregl-canvas').length, (window.__maps || []).map(m => !!m.__drawn)]")
        print(f"maps not drawn after 30 s on {page.url}: built, canvases, drawn = {seen}", file=sys.stderr)


def sign_in(browser, base: str, password: str, state: Path) -> None:
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"{base}/login")
    page.fill("#password", password)
    with page.expect_navigation():
        page.press("#password", "Enter")
    ctx.storage_state(path=str(state))
    ctx.close()


def go(page, path: str, settle: float, cuts: list, t0: float) -> None:
    """A navigation from inside the site: same-origin referrer, so the arrival does not replay.
    The time from the tap to the page's maps being drawn is noted in cuts and cut from the loop:
    a headless browser draws a map far slower than a phone does."""
    tap = time.monotonic() - t0
    with page.expect_navigation():
        page.evaluate("p => location.assign(p)", path)
    page.wait_for_load_state("networkidle")
    maps_drawn(page)
    cuts.append((tap + 0.1, time.monotonic() - t0))
    time.sleep(settle)


FRAME_TIMELINE = """top => {
  const days = [...document.querySelectorAll('.tday')];
  const at = days.findIndex(d => d.querySelector("a[href*='/charges/']") && d.textContent.includes('Lake'));
  if (at < 0) return;
  const day = days[Math.max(0, at - 1)];  // the day after it: the home charge in its small hours
  window.scrollTo(0, day.getBoundingClientRect().top + scrollY - top);
}"""


def frame_timeline(page, top: int) -> None:
    """Scroll the Timeline to the newest road charge and the day before it, so one window shows a
    drive, a home charge, a road charge and the stops between."""
    page.evaluate(FRAME_TIMELINE, top)
    time.sleep(0.3)


def glide(page, y: int, ms: int = 900) -> None:
    page.evaluate("([y, ms]) => window.scrollTo({top: y, behavior: 'smooth'})", [y, ms])
    time.sleep(ms / 1000 + 0.3)


def record_loop(browser, base: str, state: Path, scheme: str, work: Path) -> tuple[Path, float, list]:
    vid = work / f"video-{scheme}"
    ctx = context(
        browser,
        viewport=PHONE,
        device_scale_factor=SCALE,
        is_mobile=True,
        has_touch=True,
        color_scheme=scheme,
        storage_state=str(state),
        record_video_dir=str(vid),
        # Playwright's video is the viewport in CSS pixels whatever the scale; a larger frame only
        # pads it with grey (the loop once showed the phone in its top-left quarter).
        record_video_size=PHONE,
    )
    # One look at every page with a map first, on a page of its own (its video is thrown away), with
    # the outside requests (the map's style and tiles) kept as they come, so the recorded visit
    # fetches nothing from outside.
    kept: dict[str, dict] = {}

    def keep(route) -> None:
        url = route.request.url
        if url not in kept:
            r = route.fetch()
            kept[url] = {"status": r.status, "headers": r.headers, "body": r.body()}
        route.fulfill(**kept[url])

    ctx.route(lambda url: not url.startswith(base), keep)
    warm = ctx.new_page()
    warm.goto(f"{base}/timeline")
    drive = warm.locator("a[href*='/drives/']").first.get_attribute("href")
    for path in ("/", drive, "/places"):
        warm.goto(f"{base}{path}")
        warm.wait_for_load_state("networkidle")
        maps_drawn(warm)
    warm.close()
    cuts: list = []
    page = ctx.new_page()
    t0 = time.monotonic()  # the recording starts with the page
    page.goto(f"{base}/", wait_until="commit")  # a fresh visit: the arrival plays
    page.wait_for_selector("main", state="visible")
    start = time.monotonic() - t0  # the first paint: the loop starts here, not on a blank page
    page.wait_for_load_state("networkidle")
    time.sleep(max(0.5, 4.4 - (time.monotonic() - t0 - start)))  # the arrival runs 4 s
    go(page, "/timeline", 0.8, cuts, t0)
    frame_timeline(page, 110)
    time.sleep(0.6)
    go(page, page.locator("a[href*='/drives/']").first.get_attribute("href"), 0.6, cuts, t0)
    shown = page.locator(".leaflet-container").first
    if shown.count():
        glide(page, max(0, int(shown.bounding_box()["y"]) - 120))
    time.sleep(0.8)
    go(page, "/stats", 1.0, cuts, t0)
    glide(page, 420)
    go(page, "/places", 2.0, cuts, t0)
    path = page.video.path()
    ctx.close()
    return Path(path), start, cuts


def encode(raw: Path, start: float, cuts: list, out_dir: Path, scheme: str) -> None:
    off = start + 0.12
    trim = ["-ss", f"{off:.2f}", "-i", str(raw)]  # from the first paint
    # each wait for a page's maps is cut, so a tap lands on the page already drawn
    gone = "+".join(f"between(t,{a - off:.2f},{b - off:.2f})" for a, b in cuts if b - a > 0.15)
    cut = f"select='not({gone})',setpts=N/FRAME_RATE/TB," if gone else ""
    gif = out_dir / f"demo-{scheme}.gif"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            *trim,
            "-filter_complex",
            f"{cut}fps={FPS},scale={LOOP_WIDTH}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
            "-loop",
            "0",
            str(gif),
        ],
        check=True,
    )
    if scheme == "light":
        mp4 = out_dir / "demo-light.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                *trim,
                "-vf",
                f"{cut}fps=30,scale={PHONE['width']}:-2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "26",
                "-preset",
                "slow",
                "-movflags",
                "+faststart",
                "-an",
                str(mp4),
            ],
            check=True,
        )


def stills(browser, base: str, state: Path, out_dir: Path, work: Path) -> None:
    for scheme in ("light", "dark"):
        ctx = context(browser, viewport=DESKTOP, device_scale_factor=1.5, color_scheme=scheme, storage_state=str(state), reduced_motion="reduce")
        page = ctx.new_page()
        page.goto(f"{base}{lake_drive(page, base)}")
        page.wait_for_load_state("networkidle")
        maps_drawn(page)
        time.sleep(1.0)
        page.screenshot(path=str(out_dir / f"desktop-{scheme}.png"))
        ctx.close()
    # The phone shot the social card is built around.
    ctx = context(browser, viewport=PHONE, device_scale_factor=2, is_mobile=True, color_scheme="dark", storage_state=str(state), reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(f"{base}/")
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)
    page.screenshot(path=str(out_dir / "now-phone-dark.png"))
    ctx.close()


def banner_shots(browser, base: str, state: Path, out_dir: Path) -> None:
    """Now on a phone with the demo car's banner (make-demo-media.sh puts Comet mid-drive), in
    both themes. First of all the shots: the banner is of now and goes when the drive goes stale."""
    for scheme in ("light", "dark"):
        ctx = context(browser, viewport=PHONE, device_scale_factor=2, is_mobile=True, color_scheme=scheme, storage_state=str(state))
        page = ctx.new_page()
        page.goto(f"{base}/", referer=f"{base}/")  # from inside the site: no arrival
        page.wait_for_load_state("networkidle")
        try:  # the banner is drawn by the live stream a moment after the page
            page.wait_for_selector(".act", timeout=15000)
        except PlaywrightTimeout:
            print("no banner on Now: the demo car is not out", file=sys.stderr)
            ctx.close()
            return
        time.sleep(1.4)  # the roadside mid-roll, the car mid-bob
        page.screenshot(path=str(out_dir / f"now-driving-{scheme}.png"))
        ctx.close()


def lake_drive(page, base: str, month: str = "") -> str:
    """The newest run to the Lake in `month` (default the newest month with drives), the demo's
    most map-worthy drive; else the newest drive. The list shows only a week's rows, so the
    site's own destination filter does the finding."""
    page.goto(f"{base}/drives?dest=Lake" + (f"&month={month}" if month else ""))
    links = page.locator("a[href*='/drives/']")
    if not links.count():
        page.goto(f"{base}/drives")
    return links.first.get_attribute("href")


# docs/screenshots: the README's six features, each one 1280 x 800 window on a light ground, framed
# on what its page is about: name -> path. Paths starting with @ are found on the site: @drive is the
# newest Saturday run to the Lake, @last is last month, the demo's one full month (this month may be
# a few days old). The thumbnails in docs/screenshots/thumbs are these, halved (make-readme-art.sh).
SHOTS = {
    "timeline": "/timeline",
    "drive": "@drive",
    "calendar": "/calendar?month=@last",
    "stats": "/stats",
    "charging": "/charging",
    "map": "/map",
}


def screenshots(browser, base: str, state: Path, shots: Path) -> None:
    ctx = context(browser, viewport=DESKTOP, device_scale_factor=1, color_scheme="light", storage_state=str(state), reduced_motion="reduce")
    page = ctx.new_page()
    last = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    found = {"@drive": lake_drive(page, base, last)}
    for name, path in SHOTS.items():
        page.goto(f"{base}{found.get(path, path).replace('@last', last)}")
        page.wait_for_load_state("networkidle")
        maps_drawn(page)
        time.sleep(1.2)
        if name == "timeline":
            frame_timeline(page, 24)
        page.screenshot(path=str(shots / f"{name}.png"))
    ctx.close()


def main(base: str, password: str, out: str, work: str, shots: str | None = None) -> None:
    out_dir, work_dir = Path(out), Path(work)
    state = work_dir / "state.json"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        sign_in(browser, base.rstrip("/"), password, state)
        banner_shots(browser, base.rstrip("/"), state, out_dir)
        for scheme in ("light", "dark"):
            raw, start, cuts = record_loop(browser, base.rstrip("/"), state, scheme, work_dir)
            encode(raw, start, cuts, out_dir, scheme)
            shutil.rmtree(raw.parent, ignore_errors=True)
        stills(browser, base.rstrip("/"), state, out_dir, work_dir)
        if shots:
            screenshots(browser, base.rstrip("/"), state, Path(shots))
        browser.close()
    for f in (*out_dir.glob("*.png"), *(Path(shots).glob("*.png") if shots else ())):
        subprocess.run(["magick", str(f), "-strip", "-define", "png:compression-level=9", str(f)], check=True)


if __name__ == "__main__":
    main(*sys.argv[1:6])

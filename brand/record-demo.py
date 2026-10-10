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
SCALE = 2  # recorded at twice the CSS size, so text stays sharp on a high-density screen
FPS = 12
LOOP_WIDTH = 390  # the README shows the loop 260 px wide; a GIF this size stays sharp on a high-density screen at a size GitHub will carry
DESKTOP = {"width": 1280, "height": 800}
TILES_LOADED = "!document.querySelector('.leaflet-tile:not(.leaflet-tile-loaded)')"


def sign_in(browser, base: str, password: str, state: Path) -> None:
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"{base}/login")
    page.fill("#password", password)
    with page.expect_navigation():
        page.press("#password", "Enter")
    ctx.storage_state(path=str(state))
    ctx.close()


def go(page, path: str, settle: float) -> None:
    """A navigation from inside the site: same-origin referrer, so the arrival does not replay."""
    with page.expect_navigation():
        page.evaluate("p => location.assign(p)", path)
    page.wait_for_load_state("networkidle")
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


def record_loop(browser, base: str, state: Path, scheme: str, work: Path) -> tuple[Path, float]:
    vid = work / f"video-{scheme}"
    ctx = browser.new_context(
        viewport=PHONE,
        device_scale_factor=SCALE,
        is_mobile=True,
        has_touch=True,
        color_scheme=scheme,
        storage_state=str(state),
        record_video_dir=str(vid),
        record_video_size={"width": PHONE["width"] * SCALE, "height": PHONE["height"] * SCALE},
    )
    # One look at Now and Places first, on a page of its own (its video is thrown away), with the outside
    # requests (the map's tiles) kept as they come, so the recorded visit gets its map at once.
    kept: dict[str, dict] = {}

    def keep(route) -> None:
        url = route.request.url
        if url not in kept:
            r = route.fetch()
            kept[url] = {"status": r.status, "headers": r.headers, "body": r.body()}
        route.fulfill(**kept[url])

    ctx.route(lambda url: not url.startswith(base), keep)
    # tiles appear at once, not fading in over a grey ground
    ctx.add_init_script(
        "document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style'); s.textContent = '.leaflet-tile { transition: none !important; opacity: 1 !important; }'; document.head.appendChild(s); });"
    )
    warm = ctx.new_page()
    for path in ("/", "/places"):
        warm.goto(f"{base}{path}")
        warm.wait_for_load_state("networkidle")
    warm.close()
    page = ctx.new_page()
    t0 = time.monotonic()  # the recording starts with the page
    page.goto(f"{base}/", wait_until="commit")  # a fresh visit: the arrival plays
    start = time.monotonic() - t0
    page.wait_for_load_state("networkidle")
    time.sleep(max(0.5, 4.4 - (time.monotonic() - t0 - start)))  # the arrival runs 4 s
    go(page, "/timeline", 0.8)
    frame_timeline(page, 110)
    time.sleep(0.6)
    drive = page.locator("a[href*='/drives/']").first.get_attribute("href")
    go(page, drive, 0.6)
    shown = page.locator(".leaflet-container").first
    if shown.count():
        page.wait_for_function(TILES_LOADED, timeout=20000)  # no half-drawn map in the loop
        glide(page, max(0, int(shown.bounding_box()["y"]) - 120))
    time.sleep(0.8)
    go(page, "/stats", 1.0)
    glide(page, 420)
    go(page, "/places", 2.0)
    path = page.video.path()
    ctx.close()
    return Path(path), start


def encode(raw: Path, start: float, out_dir: Path, scheme: str) -> None:
    trim = ["-ss", f"{start + 0.12:.2f}", "-i", str(raw)]  # past the blank page before the first paint
    gif = out_dir / f"demo-{scheme}.gif"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            *trim,
            "-filter_complex",
            f"fps={FPS},scale={LOOP_WIDTH}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
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
                f"fps=30,scale={PHONE['width'] * SCALE}:-2",
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
        ctx = browser.new_context(viewport=DESKTOP, device_scale_factor=1.5, color_scheme=scheme, storage_state=str(state), reduced_motion="reduce")
        page = ctx.new_page()
        page.goto(f"{base}{lake_drive(page, base)}")
        page.wait_for_load_state("networkidle")
        page.wait_for_function(TILES_LOADED, timeout=20000)
        time.sleep(1.0)
        page.screenshot(path=str(out_dir / f"desktop-{scheme}.png"))
        ctx.close()
    # The phone shot the social card is built around.
    ctx = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, color_scheme="dark", storage_state=str(state), reduced_motion="reduce")
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
        ctx = browser.new_context(viewport=PHONE, device_scale_factor=2, is_mobile=True, color_scheme=scheme, storage_state=str(state))
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
    ctx = browser.new_context(viewport=DESKTOP, device_scale_factor=1, color_scheme="light", storage_state=str(state), reduced_motion="reduce")
    page = ctx.new_page()
    last = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    found = {"@drive": lake_drive(page, base, last)}
    for name, path in SHOTS.items():
        page.goto(f"{base}{found.get(path, path).replace('@last', last)}")
        page.wait_for_load_state("networkidle")
        # Every map tile loaded, then a moment for their fade-in.
        page.wait_for_function(TILES_LOADED, timeout=20000)
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
            raw, start = record_loop(browser, base.rstrip("/"), state, scheme, work_dir)
            encode(raw, start, out_dir, scheme)
            shutil.rmtree(raw.parent, ignore_errors=True)
        stills(browser, base.rstrip("/"), state, out_dir, work_dir)
        if shots:
            screenshots(browser, base.rstrip("/"), state, Path(shots))
        browser.close()
    for f in (*out_dir.glob("*.png"), *(Path(shots).glob("*.png") if shots else ())):
        subprocess.run(["magick", str(f), "-strip", "-define", "png:compression-level=9", str(f)], check=True)


if __name__ == "__main__":
    main(*sys.argv[1:6])

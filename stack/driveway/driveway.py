"""The driveway camera, read only on demand: while a car of the house is on its way home, or leaving.

The site asks `POST /watch {"seconds": n, "car": name}` when its approach model says a car is about a minute
out, and `{"leaving": true}` while the garage stands open for its car to go (Leaver). Only then are frames pulled (ffmpeg, the newest each time the detector is free) and searched for cars (YOLOX, Apache 2.0).
The cars in view when the watch starts are the parked ones and never count; the first other car
seen moving answers the request, which is what starts the garage door. Outside a watch the stream
is not opened. `snapshot` writes one frame as JPEG to stdout.

DRIVEWAY_ZONE, from .env, is the driveway itself as a polygon of frame fractions ("x,y x,y ..."): a
car counts only once the bottom middle of its box, where it meets the ground, is inside it, so a car
on the street does not start the door until it turns in. Unset, the whole frame counts.

The stream's address (with its login) is DRIVEWAY_RTSP, from .env; it is never logged or printed.
A watch that saw a car keeps its last KEPT frames under /data for DRIVEWAY_KEEP_DAYS (7; 0 keeps them all), to tune against;
each also keeps a frame every TRAIL_EVERY s of the TRAIL_S before, under street/: the car coming down the street or
getting ready to go. A watch that saw nothing keeps the same, with box.json null; and with no watch running, a frame
is taken at random every 30 min to 2 h into random/: the drive with no car coming, to tell the car from.
"""

from __future__ import annotations

import io
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

W, H = 960, 540  # frames are scaled to this before anything looks at them
SIZE = 640  # the model's input
CARS = {2: "car", 5: "bus", 7: "truck"}  # COCO classes that are a vehicle in a driveway
PERSON = 0  # COCO: someone in the driveway stops a close
SCORE = 0.35
MOVED = 0.02  # a box's centre must move this share of the frame width between first and last sight
SEEN_IN = 2  # frames a new car must be seen in before it counts
BASELINE_S = 1.0  # the first second of a watch is what is parked
CLEAR_Y = 0.9  # leaving: a car whose near edge is above this share of the frame height is all out of the garage
GARAGE_Y = 0.8  # leaving: a car first seen with its ground point this far down the frame came out of the garage, under the camera
KEPT = 50  # frames kept of a watch that saw a car, the last 10 s or so: the car turning in or pulling out, to train on
TRAIL_EVERY, TRAIL_S = 0.5, 120  # and a frame every half second of the 2 min before: it coming down the street, or getting ready to go
RANDOM_MIN_S, RANDOM_MAX_S = 1800, 7200  # between frames taken at random with no watch running: the drive as it usually is
KEEP_DAYS = float(os.environ.get("DRIVEWAY_KEEP_DAYS") or 7)  # 0 keeps every watch: training data until the car is matched
MODEL = os.environ.get("DRIVEWAY_MODEL", "/app/yolox_s.onnx")
DATA = Path(os.environ.get("DRIVEWAY_DATA", "/data"))


# ---- pure: boxes


def zone(text: str | None) -> list | None:
    """DRIVEWAY_ZONE ("x,y x,y x,y ...", fractions of the frame) as frame pixels; None for the whole frame."""
    points = [tuple(float(v) for v in p.split(",")) for p in (text or "").split()]
    if len(points) < 3 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in points):
        return None
    return [(x * W, y * H) for x, y in points]


def inside(poly: list | None, x: float, y: float) -> bool:
    if poly is None:
        return True
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def nms(boxes: list, thresh: float = 0.45) -> list:
    out = []
    for b in sorted(boxes, key=lambda b: -b[4]):
        if all(iou(b, k) < thresh for k in out):
            out.append(b)
    return out


def decode(pred, scale: float):
    """YOLOX's raw output (1, N, 85) for a SIZE x SIZE input into vehicle and person boxes in frame
    pixels: [x1, y1, x2, y2, score, class]."""
    import numpy as np

    grids, strides = [], []
    for s in (8, 16, 32):
        n = SIZE // s
        yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((n * n, 1), s))
    grid, stride = np.concatenate(grids), np.concatenate(strides)
    p = pred[0]
    xy = (p[:, :2] + grid) * stride
    wh = np.exp(p[:, 2:4]) * stride
    scores = p[:, 4:5] * p[:, 5:]
    out = []
    for cls in (*CARS, PERSON):
        keep = scores[:, cls] > SCORE
        for (cx, cy), (w, h), sc in zip(xy[keep], wh[keep], scores[keep, cls]):
            out.append([float((cx - w / 2) / scale), float((cy - h / 2) / scale), float((cx + w / 2) / scale), float((cy + h / 2) / scale), float(sc), cls])
    return nms(out)


class Tracker:
    """A watch's view of the cars: those of the first BASELINE_S are parked; a car not over one of
    them, seen in SEEN_IN frames and moved MOVED of the width since first seen, has arrived, once
    it stands in the zone (if there is one)."""

    def __init__(self, area: list | None = None) -> None:
        self.area = area
        self.parked: list = []
        self.started: float | None = None
        self.tracks: list[dict] = []  # {box, first, n}

    def step(self, t: float, boxes: list) -> list | None:
        if self.started is None:
            self.started = t
        if t - self.started < BASELINE_S:
            self.parked += [b for b in boxes if b[5] in CARS and all(iou(b, p) < 0.5 for p in self.parked)]
            return None
        for b in boxes:
            if b[5] not in CARS or any(iou(b, p) > 0.5 for p in self.parked):
                continue
            track = max(self.tracks, key=lambda tr: iou(tr["box"], b), default=None)
            if track is None or iou(track["box"], b) < 0.2:
                self.tracks.append({"box": b, "first": b, "n": 1})
                continue
            track["box"], track["n"] = b, track["n"] + 1
            moved = abs((b[0] + b[2]) / 2 - (track["first"][0] + track["first"][2]) / 2) + abs((b[1] + b[3]) / 2 - (track["first"][1] + track["first"][3]) / 2)
            if track["n"] >= SEEN_IN and moved >= MOVED * W and inside(self.area, (b[0] + b[2]) / 2, b[3]):
                return b
        return None


class Leaver:
    """A departure: those of the first BASELINE_S are parked; a car first seen low in the frame, coming
    out of the garage under the camera, answers once seen SEEN_IN times, moved up, with its near edge
    (the box's bottom) above CLEAR_Y: the camera is over the door, so the whole car is out and the door
    can close, moving or stopped in the drive (the button in the car stops it
    if that was not wanted). The people standing in the zone at that moment go with the answer:
    someone in the driveway stops the close, someone on the sidewalk or the street does not, nor the
    driver seen through the glass."""

    def __init__(self, area: list) -> None:
        self.area = area
        self.parked: list = []
        self.started: float | None = None
        self.tracks: list[dict] = []

    def step(self, t: float, boxes: list) -> tuple[list, list] | None:
        if self.started is None:
            self.started = t
        cars = [b for b in boxes if b[5] in CARS]
        if t - self.started < BASELINE_S:
            self.parked += [b for b in cars if all(iou(b, p) < 0.5 for p in self.parked)]
            return None
        for b in cars:
            if any(iou(b, p) > 0.5 for p in self.parked):
                continue
            track = max(self.tracks, key=lambda tr: iou(tr["box"], b), default=None)
            if track is None or iou(track["box"], b) < 0.2:
                if b[3] >= GARAGE_Y * H:
                    self.tracks.append({"box": b, "first": b, "n": 1})
                continue
            track["box"], track["n"] = b, track["n"] + 1
            if track["n"] >= SEEN_IN and track["first"][3] - b[3] >= MOVED * H and b[3] < CLEAR_Y * H:
                feet = [((p[0] + p[2]) / 2, p[3]) for p in boxes if p[5] == PERSON]
                people = [f for f in feet if inside(self.area, *f) and not (b[0] <= f[0] <= b[2] and b[1] <= f[1] <= b[3])]
                return b, people
        return None


# ---- the camera


def url() -> str:
    u = os.environ.get("DRIVEWAY_RTSP")
    if not u:
        raise SystemExit("DRIVEWAY_RTSP is not set")
    return u


def quiet(text: str) -> str:
    """ffmpeg names the URL in its errors: never with its login."""
    return text.replace(os.environ.get("DRIVEWAY_RTSP") or "\0", "rtsp://…")


def snapshot(timeout: int = 20) -> bytes:
    run = subprocess.run(
        ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", url(), "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "-q:v", "3", "-"], capture_output=True, timeout=timeout, check=False
    )
    if run.returncode or not run.stdout:
        raise SystemExit(f"no frame (ffmpeg exit {run.returncode}): " + quiet(run.stderr.decode(errors="replace"))[-200:])
    return run.stdout


class Newest:
    """The newest whole frame from a byte stream, read as fast as it comes. A frame the detector had no
    time for is dropped, never queued: queued frames put what it looks at seconds behind the camera
    (2026-10-19: the trigger frame was 5 s old after a 2-minute watch at 4.9 of 5 fps)."""

    def __init__(self, stream, size: int) -> None:
        self.stream, self.size = stream, size
        self.ready = threading.Condition()
        self.frame: tuple[float, bytes] | None = None
        self.ended = False
        self.dropped = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            raw = self.stream.read(self.size)
            with self.ready:
                if len(raw) < self.size:
                    self.ended = True
                else:
                    self.dropped += self.frame is not None
                    self.frame = (time.time(), raw)
                self.ready.notify_all()
            if self.ended:
                return

    def take(self, stop: threading.Event) -> tuple[float, bytes] | None:
        """The newest frame not yet taken (waiting for one), or None once the stream ends or stop is set."""
        with self.ready:
            while self.frame is None and not self.ended and not stop.is_set():
                self.ready.wait(0.5)
            frame, self.frame = self.frame, None
            return frame


def frames(stop: threading.Event):
    """(time, RGB frame as numpy H x W x 3), the newest of the stream each time, until stop is set or the stream ends.
    Every frame the camera sends is decoded and the detector takes the newest: an fps filter held frames back
    (measured on the Eufy's clock 2026-10-20: 4.3 s behind with fps=5, 2.3 s without, the camera's own)."""
    import numpy as np

    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-flags", "low_delay", "-an", "-i", url()]
        + ["-fps_mode", "passthrough", "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    newest = Newest(proc.stdout, W * H * 3)
    try:
        while not stop.is_set():
            got = newest.take(stop)
            if got is None:
                return
            yield got[0], np.frombuffer(got[1], np.uint8).reshape(H, W, 3)
    finally:
        proc.kill()


class Detector:
    def __init__(self) -> None:
        import onnxruntime

        self.session = onnxruntime.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
        self.name = self.session.get_inputs()[0].name

    def boxes(self, frame) -> list:
        import numpy as np
        from PIL import Image

        scale = SIZE / max(frame.shape[0], frame.shape[1])
        img = Image.fromarray(frame).resize((int(frame.shape[1] * scale), int(frame.shape[0] * scale)))
        canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)
        canvas[: img.height, : img.width] = np.asarray(img)[:, :, ::-1]  # YOLOX was trained on BGR
        x = canvas.transpose(2, 0, 1)[None].astype(np.float32)
        return decode(self.session.run(None, {self.name: x})[0], scale)


def jpeg(frame) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def keep(frames_seen: list, box: list | None, car: str | None = None, way: str | None = None, trail: list | None = None) -> str:
    """The last KEPT frames of a watch that saw a car, for tuning, with the car the site was watching for
    (car.txt): what the car is told apart from other cars by, and way.txt "out" for one leaving. `trail` is the
    (time, JPEG) frames of the approach, kept under street/ named by seconds before the car was seen: what the car
    is found down the street by. `box` None is a watch that saw nothing (box.json null): what it was watching for
    did not come, or not where it looked. Older watches are deleted."""
    now = datetime.now(timezone.utc)
    for old in DATA.glob("2*/") if KEEP_DAYS > 0 else ():
        try:
            if now - datetime.fromisoformat(old.name) > timedelta(days=KEEP_DAYS):
                shutil.rmtree(old)
        except (ValueError, OSError):
            continue
    where = DATA / now.replace(microsecond=0).isoformat()
    where.mkdir(parents=True, exist_ok=True)
    for i, (t, frame) in enumerate(frames_seen[-KEPT:]):
        (where / f"{i}.jpg").write_bytes(jpeg(frame))
    if trail:
        (where / "street").mkdir(exist_ok=True)
        last = frames_seen[-1][0] if frames_seen else trail[-1][0]
        for t, data in trail:
            (where / "street" / f"{max(0.0, last - t):06.1f}.jpg").write_bytes(data)
    (where / "box.json").write_text(json.dumps(box))
    if car:
        (where / "car.txt").write_text(car)
    if way:
        (where / "way.txt").write_text(way)
    return where.name


class Watch:
    """One watch at a time; a second request while one runs waits on the same answer."""

    def __init__(self, detector_factory=Detector, frame_source=frames) -> None:
        self.lock = threading.Lock()
        self.detector_factory, self.frame_source = detector_factory, frame_source
        self.detector = None
        self.running: dict | None = None

    def run(self, seconds: float, store: bool = True, car: str | None = None, leaving: bool = False) -> dict:
        """Watch, or wait on the watch already running (its frames are kept under the car it was first asked for).
        `leaving`: for a car going out, not coming in; a watch the other way already running answers busy."""
        with self.lock:
            if self.running is not None and self.running["leaving"] != leaving:
                return {"seen": False, "why": "busy", "frames": 0}
            if self.running is None:
                self.running = {"done": threading.Event(), "stop": threading.Event(), "answer": None, "car": car, "leaving": leaving}
                threading.Thread(target=self._watch, args=(self.running, seconds, store), daemon=True).start()
            mine = self.running
        mine["done"].wait(seconds + 30)
        return mine["answer"] or {"seen": False, "why": "no answer"}

    def stop(self) -> None:
        with self.lock:
            if self.running:
                self.running["stop"].set()

    def _watch(self, w: dict, seconds: float, store: bool) -> None:
        t0, n, answer, recent, trail, timed_out = time.time(), 0, None, [], [], False
        try:
            self.detector = self.detector or self.detector_factory()
            area = zone(os.environ.get("DRIVEWAY_ZONE"))
            if w["leaving"] and area is None:
                raise ValueError("a car leaving needs DRIVEWAY_ZONE")
            tracker = Leaver(area) if w["leaving"] else Tracker(area)
            for t, frame in self.frame_source(w["stop"]):
                n += 1
                recent = (recent + [(t, frame)])[-KEPT:]
                if store and (not trail or t - trail[-1][0] >= TRAIL_EVERY):
                    trail = (trail + [(t, jpeg(frame))])[-int(TRAIL_S / TRAIL_EVERY) :]
                hit = tracker.step(t, self.detector.boxes(frame))
                if w["leaving"] and hit:
                    hit, people = hit
                if hit:
                    answer = {
                        "seen": True,
                        "at": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                        "box": [round(v) for v in hit[:4]],
                        "score": round(hit[4], 2),
                        "frames": n,
                        "parked": len(tracker.parked),
                    }
                    if w["leaving"]:
                        answer.update(leaving=True, people=len(people))
                    if store:
                        answer["kept"] = keep(recent, answer["box"], w.get("car"), "out" if w["leaving"] else None, trail)
                    break
                if time.time() - t0 > seconds:
                    timed_out = True
                    break
            why = "stopped" if w["stop"].is_set() else "timed out" if timed_out else "stream ended" if n else "no frames"
            if answer is None and store and n and not w["stop"].is_set():
                keep(recent, None, w.get("car"), "out" if w["leaving"] else None, trail)
            answer = answer or {"seen": False, "why": why, "frames": n, "parked": len(tracker.parked)}
        except Exception as err:  # noqa: BLE001 - the site falls back; the answer says why
            answer = {"seen": False, "why": f"{type(err).__name__}: {quiet(str(err))[:120]}", "frames": n}
        answer["fps"] = round(n / max(0.1, time.time() - t0), 1)
        with self.lock:
            w["answer"] = answer
            self.running = None
        w["done"].set()


WATCH = Watch()


def random_frames(stop: threading.Event, wait=random.uniform, grab=snapshot) -> None:
    """Now and then, at a random time with no watch running, one frame of the drive into DATA/random/: what it
    looks like with no car coming or going, the rejects a model learns the car apart from. Never deleted by age."""
    while not stop.wait(wait(RANDOM_MIN_S, RANDOM_MAX_S)):
        if WATCH.running is not None:
            continue
        try:
            data = grab()
        except BaseException:  # noqa: BLE001 - snapshot exits on no frame; a missed sample is nothing
            continue
        (DATA / "random").mkdir(parents=True, exist_ok=True)
        (DATA / "random" / f"{datetime.now(timezone.utc).replace(microsecond=0).isoformat()}.jpg").write_bytes(data)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/watch":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                leaving = body.get("leaving") is True
                seconds = min(600.0 if leaving else 300.0, max(5.0, float(body.get("seconds") or 90)))
            except (ValueError, TypeError):
                return self._send(400, {"error": "seconds"})
            car = str(body.get("car") or "")[:40] or None
            return self._send(200, WATCH.run(seconds, car=car, leaving=leaving))
        if self.path == "/stop":
            WATCH.stop()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "no such path"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            return self._send(200, {"ok": True, "watching": WATCH.running is not None})
        self._send(404, {"error": "no such path"})

    def log_message(self, *args) -> None:  # requests carry nothing worth a line, and never the stream
        pass


if __name__ == "__main__":
    arg = sys.argv[1:2]
    if arg == ["snapshot"]:
        sys.stdout.buffer.write(snapshot())
    elif arg == ["boxes"]:  # the vehicles in one frame, and how long the model took
        stop = threading.Event()
        t, frame = next(frames(stop))
        stop.set()
        d = Detector()
        t0 = time.time()
        found = d.boxes(frame)
        print(json.dumps({"ms": round((time.time() - t0) * 1000), "boxes": [[round(v) for v in b[:4]] + [round(b[4], 2), CARS[b[5]]] for b in found]}))
    elif arg == ["try"]:  # watch for N seconds from the command line, keeping nothing
        print(json.dumps(Watch().run(float(sys.argv[2]) if len(sys.argv) > 2 else 20, store=False)))
    elif arg == ["serve"] or not arg:
        threading.Thread(target=random_frames, args=(threading.Event(),), daemon=True).start()
        ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
    else:
        raise SystemExit("usage: driveway.py [serve | snapshot | try SECONDS]")

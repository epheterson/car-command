#!/usr/bin/env bash
# Record the README's demo media and the social card from the demo car, never from real data:
#   brand/media/demo-light.gif, demo-dark.gif    the phone loop (Now with the arrival, Timeline, a drive, Stats, Places)
#   brand/media/demo-light.mp4                   the same loop as video, for anywhere a GIF is too heavy
#   brand/media/desktop-light.png, -dark.png     one desktop still of a drive
#   brand/media/now-driving-light.png, -dark.png Now on a phone with Comet's banner, mid-drive to Work
#   brand/social-preview.png                     the 1280 x 640 card (brand/social-preview.html around a phone shot)
#   docs/screenshots/*.png, and their thumbs     the README's six feature shots, 1280 x 800 (then make-readme-art.sh)
#
#   brand/make-demo-media.sh              a throwaway site: Postgres in a temp dir, the demo car, then the recording
#   BASE_URL=http://host:3044 PASSWORD=... brand/make-demo-media.sh
#                                         record a site already running the demo car instead
#                                         (make one-container-run && make one-container-demo)
#
# Needs uv, ImageMagick and an ffmpeg built with libx264. The throwaway site runs from a venv in a temp dir with
# Postgres from the pgserver wheel; everything it starts is stopped and removed on exit.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/brand/media"
mkdir -p "$OUT"
for t in uv ffmpeg magick; do command -v "$t" >/dev/null || { echo "needs $t" >&2; exit 1; }; done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/cc-demo.XXXXXX")"
SITE_PID=""
cleanup() {
  [ -n "$SITE_PID" ] && kill "$SITE_PID" 2>/dev/null && wait "$SITE_PID" 2>/dev/null || true
  [ -x "$WORK/venv/bin/python" ] && [ -d "$WORK/pg" ] && "$WORK/venv/bin/python" -c "
import pgserver; pgserver.get_server('$WORK/pg', cleanup_mode='delete').cleanup()" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

uv venv -q -p 3.12 "$WORK/venv"
PY="$WORK/venv/bin/python"
VIRTUAL_ENV="$WORK/venv" uv pip install -q playwright==1.63.0
"$PY" -m playwright install chromium >/dev/null

if [ -z "${BASE_URL:-}" ]; then
  VIRTUAL_ENV="$WORK/venv" uv pip install -q -r "$HERE/stack/web/requirements.txt" -r "$HERE/stack/cc/requirements.txt" psycopg2-binary==2.9.13 pgserver==0.1.4
  DATABASE_URL="$("$PY" -c "import pgserver; print(pgserver.get_server('$WORK/pg', cleanup_mode=None).get_uri())")"
  PASSWORD="demo-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
  PORT="${PORT:-38044}"
  export DATABASE_URL CC_PASSWORD="$PASSWORD" CC_SECRET="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')" \
         REST_USER=demo REST_PASSWORD=demo CC_REST_URL=http://127.0.0.1:9 TZ=America/Chicago
  (cd "$HERE/stack/cc" && "$PY" init_db.py)
  BASE_URL="http://127.0.0.1:$PORT"
  start_site() {
    (cd "$HERE/stack/web" && exec "$PY" -m uvicorn app:server --host 127.0.0.1 --port "$PORT" --log-level warning) &
    SITE_PID=$!
    for _ in $(seq 90); do curl -fsS "$BASE_URL/health" >/dev/null 2>&1 && return; sleep 1; done
    echo "the site did not answer on $BASE_URL" >&2; exit 1
  }
  # The site makes its own tables as it starts; the demo steps (make demo) come after.
  start_site
  # The demo car's six weeks, ending this morning rather than on demo.py's fixed date, so Now
  # and this month's Stats read as a car in use.
  (cd "$HERE/stack/web" && "$PY" -c "
import demo, gzip, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
end = datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None, hour=6, minute=0, second=0, microsecond=0)
end = end - timedelta(days=1) if end + timedelta(minutes=45) > datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None) else end  # before 6 am the history's end would be in the future
gzip.open(sys.argv[1], 'wb').write(demo.export_csv(demo.generate(end=end)).encode())" "$WORK/demo.csv.gz" \
     && "$PY" -c "import app, demo; demo.prepare(app)" \
     && "$PY" teslafi_load.py "$WORK/demo.csv.gz")
  # The demo car is fictional, so its maps may have backgrounds from the public tile service
  # for the recording; a real install starts with them off. The site reads the switch as it starts.
  "$PY" -c "
import psycopg, os
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    c.execute(\"INSERT INTO cc_settings (key, value) VALUES ('public_tiles', 'true') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value\")"
  kill "$SITE_PID"; wait "$SITE_PID" 2>/dev/null || true
  start_site
  # Comet seven minutes into a drive to Work, its destination set, so Now opens on its banner.
  # Written last: the banner is of now, and goes once the newest row is a quarter hour old.
  (cd "$HERE/stack/web" && "$PY" -c "
import demo, os, psycopg
from zoneinfo import ZoneInfo
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    print(demo.put_mid_drive(c, ZoneInfo('America/Chicago')), 'feed rows: Comet on its way to Work')")
fi

"$PY" "$HERE/brand/record-demo.py" "$BASE_URL" "${PASSWORD:?PASSWORD is the site password}" "$OUT" "$WORK" "$HERE/docs/screenshots"
# The social card around the new phone shot, and the README's feature grid cut from the screenshots.
"$HERE/brand/make-readme-art.sh"
ls -l "$OUT" "$HERE/brand/social-preview.png"

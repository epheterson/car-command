#!/usr/bin/env bash
# .github/scripts/maps-e2e.sh — the maps add-on against a real, tiny region (Monaco):
# ensure -> download -> merge -> Valhalla tiles -> place index -> reverse and candidates.
#
# Geofabrik's index names Monaco "monaco" (parent "europe"), not "europe/monaco" -- that is
# the id this service's /regions/ensure returns and the one we check for below.
set -euo pipefail
docker run -d --name maps -p 8090:8090 -v maps-e2e:/data cc-maps:ci
for i in $(seq 1 30); do curl -sf localhost:8090/status >/dev/null && break; sleep 2; done
curl -sf -X POST localhost:8090/regions/ensure -H 'Content-Type: application/json' -d '{"lat":43.7384,"lon":7.4246,"reason":"ci"}' | tee /tmp/ensure.json
echo

# What the add-on knows when this gives up: its status, its own log, and the build's.
explain() {
  echo "::error::$1"
  echo "--- /status"; curl -s localhost:8090/status || true; echo
  echo "--- docker logs maps (tail)"; docker logs maps 2>&1 | tail -60 || true
  echo "--- build logs"; docker exec maps sh -c 'tail -n 40 /data/builds/*/build.log' 2>&1 || true
  exit 1
}

# Monaco is in Provence's outline too; the add-on takes Monaco when its size is known.
grep -q '"monaco"' /tmp/ensure.json || explain "ensure chose another region than Monaco"

# Monaco is tiny but the build loop only ticks every 60s; allow a tick plus download and build.
# A download that failed against Geofabrik (their servers stall single requests when loaded)
# backs the add-on off for an hour; a person would press Retry, so this does the same, a few
# times, and fails on anything that is not a download.
v=""
retries=0
for i in $(seq 1 90); do
  st=$(curl -sf localhost:8090/status || echo '{}')
  v=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("version") or "")' "$st")
  [ -n "$v" ] && break
  # One line: the state, whether it is backing off, and the error (which may hold spaces).
  read -r building waiting error < <(python3 -c 'import json,sys; s=json.loads(sys.argv[1]); print(s.get("building") or "-", "yes" if s.get("wait_until") else "no", s.get("error") or "-")' "$st")
  # Failed and backing off is a fresh failure. A retry clears the backoff but not the status,
  # so the same failure is not retried again while the next tick comes round.
  if [ "$building" = "failed" ] && [ "$waiting" = "yes" ]; then
    case "$error" in
      download:* | "HTTP "*)
        [ "$retries" -lt 3 ] || explain "the download kept failing: $error"
        retries=$((retries + 1))
        echo "download failed ($error); retry $retries"
        curl -sf -X POST localhost:8090/retry -H 'Content-Type: application/json' -d '{}' >/dev/null
        ;;
      *) explain "the build failed: $error" ;;
    esac
  fi
  sleep 10
done
[ -n "$v" ] || explain "build never finished"
echo "built version $v"
sleep 5
curl -sf 'localhost:8090/reverse?lat=43.7384&lon=7.4246&format=jsonv2' | tee /tmp/rev.json
echo
python3 -c 'import json; a=json.load(open("/tmp/rev.json"))["address"]; assert a.get("road"), a; assert a.get("city") or a.get("town") or a.get("village"), a'
curl -sf 'localhost:8090/candidates?lat=43.7384&lon=7.4246&limit=10' | python3 -c 'import json,sys; b=json.load(sys.stdin); assert b["candidates"], b'
echo "maps e2e ok"

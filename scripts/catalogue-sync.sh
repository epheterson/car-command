#!/bin/sh
# Keep the site and the connector on the same catalogue of other-brand connectors.
#
# Both images ship connectors.json (stack/shared/connectors.json, copied into each build
# context). The site offers what its copy lists and the connector installs what its copy
# pins; a deploy that rebuilds only one of them leaves them disagreeing about releases. Run
# on the host after a deploy (make web and make deploy do): every running container whose
# copy is not shared/connectors.json is rebuilt, with --no-deps.
#
#   scripts/catalogue-sync.sh            rebuild what differs
#   scripts/catalogue-sync.sh --check    only say what differs; exit 1 if anything does
set -eu
cd "$(dirname "$0")/.."
project="${COMPOSE_PROJECT_NAME:-carcommand}"

digest() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

want=$(digest < shared/connectors.json)
stale=""
for svc in cc web; do
    have=$(docker exec "$project-$svc" cat /app/connectors.json 2>/dev/null | digest) || have=""
    # A container that is not running has nothing to disagree with; it builds from the files when it starts.
    if docker exec "$project-$svc" true >/dev/null 2>&1 && [ "$have" != "$want" ]; then
        stale="$stale $svc"
    fi
done
if [ -z "$stale" ]; then
    echo "connectors.json: the site and the connector agree"
    exit 0
fi
if [ "${1:-}" = "--check" ]; then
    echo "connectors.json differs in:$stale"
    exit 1
fi
for svc in $stale; do
    echo "connectors.json changed: rebuilding $svc so both read the same catalogue"
    docker compose up -d --build --no-deps "$svc"
done

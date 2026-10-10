#!/usr/bin/env bash
# Choose the first password, the way the setup page asks for it: with the setup code the site logs at start,
# because a request through the published port arrives from the Docker bridge, not from the machine itself.
. "$(dirname "$0")/lib.sh"

# The container publishing this port, and the newest setup code in its log. A build from before the code was
# required logs it too and ignores it, so one script serves the upgrade jobs' old and new sites.
name=$(docker ps --filter "publish=${PORT}" --format '{{.Names}}' | head -1)
code=$(docker logs "$name" 2>&1 | grep -oE 'setup code [A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}' | tail -1 | awk '{print $3}' || true)
[ -n "$code" ] || {
    echo "::error::no setup code in the log of the container on port ${PORT} (${name:-none})"
    exit 1
}

rm -f "$JAR"
status=$(curl -sS -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' \
    --data-urlencode "password=${PASSWORD}" --data-urlencode "confirm=${PASSWORD}" --data-urlencode "code=${code}" \
    --data-urlencode "csrf_token=$(token_from /setup)" "$BASE/setup")
[ "$status" = "303" ] || {
    echo "::error::choosing a password answered $status, expected 303"
    exit 1
}
echo "password set on $BASE"

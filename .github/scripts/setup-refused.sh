#!/usr/bin/env bash
# A stranger at a fresh site: the setup page renders, and choosing a password without the setup code is refused,
# because the published port shows every caller, this runner included, as the compose network's gateway (inside the
# container's own subnet) or the Docker bridge, never as a device on the home network or the machine itself.
. "$(dirname "$0")/lib.sh"

rm -f "$JAR"
page=$(curl -sS -b "$JAR" -c "$JAR" "$BASE/setup")
grep -q "Choose a password" <<<"$page" || {
    echo "::error::the setup page did not render"
    exit 1
}
status=$(curl -sS -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' \
    --data-urlencode "password=${PASSWORD}" --data-urlencode "confirm=${PASSWORD}" \
    --data-urlencode "csrf_token=$(token_from /setup)" "$BASE/setup")
[ "$status" = "403" ] || {
    echo "::error::a password without the setup code answered $status, expected 403"
    exit 1
}
echo "setup refused without the code on $BASE"

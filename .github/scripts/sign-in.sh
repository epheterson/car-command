#!/usr/bin/env bash
# Sign in with the password chosen earlier. Used after an upgrade, where the whole point
# is that the stored credential survived it.
. "$(dirname "$0")/lib.sh"

rm -f "$JAR"
code=$(curl -sS -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' \
    -d "password=${PASSWORD}&csrf_token=$(token_from /login)" "$BASE/login")
[ "$code" = "303" ] || {
    echo "::error::signing in answered $code — the stored password did not survive the upgrade"
    exit 1
}
echo "signed in to $BASE"

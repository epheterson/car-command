#!/usr/bin/env bash
# A named place: the simplest row the site both writes and reads back.
. "$(dirname "$0")/lib.sh"
NAME="${2:?usage: $0 PORT NAME}"

token=$(token_from /places)
[ -n "$token" ] || {
    echo "::error::no form token on /places"
    exit 1
}
code=$(curl -sS -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' \
    --data-urlencode "name=${NAME}" \
    -d "latitude=40.71&longitude=-74.00&radius_m=150&csrf_token=${token}" \
    "$BASE/places")
[ "$code" = "303" ] || {
    echo "::error::adding a place answered $code, expected 303"
    exit 1
}
has_place "$NAME" || {
    echo "::error::wrote '${NAME}' and could not read it back"
    exit 1
}
echo "wrote and read back '${NAME}'"

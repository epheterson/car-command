# Shared bits for the install-path checks. One cookie jar per port, so a session lives
# across these scripts the way a browser's would.
set -euo pipefail

PORT="${1:?usage: $0 PORT [...]}"
BASE="http://localhost:${PORT}"
JAR="${RUNNER_TEMP:-/tmp}/cc-jar-${PORT}"
PASSWORD="a-good-long-password"

# The token a form on this page would carry. Read through the jar, so it is bound to the
# session the jar holds — which is exactly what the server checks it against. The whole
# page first: a reader that stops early would cut the curl off, and pipefail would see it.
token_from() {
    local page
    page=$(curl -sS -b "$JAR" -c "$JAR" "$BASE$1")
    grep -o 'name="csrf_token" value="[^"]*"' <<<"$page" | head -1 | sed 's/.*value="//;s/"//'
}

# Whether a place by exactly this name is in the list the Places page draws. The page
# carries only its frame; the list arrives from /places/index.json, so that is what is read.
# A build from before that endpoint answers 404, and its page still lists the names.
has_place() {
    local body
    if body=$(curl -sSf -b "$JAR" "$BASE/places/index.json"); then
        NAME="$1" python3 -c 'import json, os, sys
names = {p.get("n") for p in json.load(sys.stdin)["places"]}
sys.exit(0 if os.environ["NAME"] in names else 1)' <<<"$body"
    else
        body=$(curl -sSf -b "$JAR" "$BASE/places") || return 1
        grep -qF "$1" <<<"$body"
    fi
}

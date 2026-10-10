#!/usr/bin/env bash
# First run: write stack/.env with generated passwords so nothing has to be hand-edited.
#
# It refuses to touch an existing .env. That file is the only copy of the database
# password, and a stack whose database password changes under it is a stack with an
# unreadable volume — so overwriting it is not something a script gets to decide.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
env_file="$here/stack/.env"
example="$here/stack/.env.example"
secrets="$here/stack/secrets"

die() {
    echo "setup: $*" >&2
    exit 1
}

[[ -f $example ]] || die "cannot find stack/.env.example — run this from a checkout"

# Where to open the site and, if it asks, the setup code. The code is derived from the signing key the site makes at
# its first start, so it can be printed only once the site has run; before that, this says how to get it.
open_hint() {
    local port code
    port=$(sed -n 's/^CC_PORT=//p' "$env_file" 2>/dev/null | tail -1)
    port=${port##*:}
    code=""
    if command -v docker >/dev/null 2>&1; then
        code=$(docker compose -f "$here/stack/docker-compose.yml" exec -T web python3 setup_code.py 2>/dev/null | grep -oE '[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}' | head -1 || true)
    fi
    if [[ -n $code ]]; then
        echo "Open http://localhost:${port:-3044} and, if asked, use setup code $code"
    else
        echo "Open http://localhost:${port:-3044} and, if asked for a setup code, get it with:"
        echo "  docker compose -f stack/docker-compose.yml exec web python3 setup_code.py"
    fi
}

if [[ -f $env_file ]]; then
    echo "stack/.env already exists. Nothing to do."
    echo "To start over, move it aside yourself — it holds the password to your database volume."
    open_hint
    exit 0
fi

command -v openssl >/dev/null || die "openssl is needed to generate passwords"

# Every CHANGE_ME becomes its own secret, one at a time: a single value reused across
# the database, the web UI and the REST API would make three doors into one.
: >"$env_file"
chmod 600 "$env_file"
while IFS= read -r line; do
    if [[ $line == *CHANGE_ME* ]]; then
        line="${line/CHANGE_ME/$(openssl rand -hex 24)}"
    fi
    # The key that seals sources kept in the app: 32 random bytes, URL-safe base64 (a Fernet key).
    if [[ $line == *GENERATE_KEY* ]]; then
        line="${line/GENERATE_KEY/$(openssl rand -base64 32 | tr '+/' '-_')}"
    fi
    printf '%s\n' "$line" >>"$env_file"
done <"$example"

mkdir -p "$secrets"
chmod 700 "$secrets"

cat <<EOF
Wrote stack/.env (mode 600) with generated passwords, and stack/secrets/ (mode 700).

Next:
  docker compose -f stack/docker-compose.yml up -d db web
      The site on http://localhost:3044. It will ask you to choose a password.
      That is enough to import history from TeslaFi or TeslaMate.

  To log a car live, put its credentials in stack/secrets/, adjust the connector
  block in stack/config/carconnectivity.json.template, and add the cc service:
      docker compose -f stack/docker-compose.yml up -d

Nothing in stack/.env or stack/secrets/ is ever committed or copied by make deploy.
EOF
open_hint

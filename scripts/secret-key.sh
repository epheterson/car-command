#!/usr/bin/env bash
# Give an existing install the key that seals sources kept in the app. Appends one line to
# .env after a dated copy; never rewrites the file, and does nothing when a key is there.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
env_file="$here/stack/.env"; [[ -f $env_file ]] || env_file="$here/.env"
[[ -f $env_file ]] || { echo "secret-key: no .env here" >&2; exit 1; }
if grep -Eq '^CC_SECRET_KEY=.+' "$env_file"; then
    echo "CC_SECRET_KEY is already set. Nothing to do."
    exit 0
fi
cp -p "$env_file" "$env_file.bak-$(date +%Y%m%d-%H%M%S)"
printf '\n# Seals the sources kept in the app. Keep it with this file; it is in no database backup.\nCC_SECRET_KEY=%s\n' "$(openssl rand -base64 32 | tr '+/' '-_')" >>"$env_file"
echo "Added CC_SECRET_KEY to $(basename "$env_file") (a copy of the old file sits beside it). Restart the site: docker compose up -d --no-deps web"

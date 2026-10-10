#!/usr/bin/env bash
# Dump the database to a file. Safe to run while everything is up.
#
# pg_dump rather than a tar of the volume: a dump restores into a different Postgres
# version, on a different machine, without stopping anything, and it can be read by a
# human when it has to be. The volume tar in infra/docker-backup.sh stays as the
# whole-machine backup; this is the one for the data.
#
# Usage: scripts/backup.sh [DIRECTORY]     (default: ./backups)
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
# In a checkout the compose file is under stack/; on a deployed host the contents of
# stack/ are the deploy directory itself. Accept either rather than making the operator
# care which one they are standing in.
if [[ -f "$here/stack/docker-compose.yml" ]]; then
    compose="$here/stack/docker-compose.yml"
    env_file="$here/stack/.env"
else
    compose="$here/docker-compose.yml"
    env_file="$here/.env"
fi
out_dir="${1:-$here/backups}"

die() {
    echo "backup: $*" >&2
    exit 1
}

[[ -f $env_file ]] || die "no .env beside the compose file — run make setup first"
[[ -f $compose ]] || die "no docker-compose.yml in $here or $here/stack"

# shellcheck disable=SC1090
db_name=$(grep -E '^DB_NAME=' "$env_file" | cut -d= -f2-)
db_user=$(grep -E '^DB_USER=' "$env_file" | cut -d= -f2-)
[[ -n ${db_name:-} && -n ${db_user:-} ]] || die "DB_NAME or DB_USER missing from stack/.env"

# Docker before anything that needs it. Without this check the next line's failure was
# reported as "the db container is not running", which sent whoever read it to look at a
# database that was running perfectly well. A tool that is not on PATH and a service that
# is down are different problems and deserve different sentences.
command -v docker >/dev/null || die "docker is not on PATH (a non-interactive shell often has a shorter one than your terminal)"
docker compose version >/dev/null 2>&1 || die "docker is installed but 'docker compose' does not run — check that the daemon is up and the compose plugin is present"

docker compose -f "$compose" ps --status running --services 2>/dev/null | grep -qx db \
    || die "the db container is not running"

mkdir -p "$out_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="$out_dir/carcommand-$stamp.dump"

# -Fc is the custom format: compressed, and pg_restore can read one table out of it.
# Written to a temporary name first, so an interrupted dump never looks like a good one.
docker compose -f "$compose" exec -T db pg_dump -U "$db_user" -d "$db_name" -Fc >"$file.partial"
mv "$file.partial" "$file"

# A dump that cannot be listed is not a backup, so this is checked now rather than on the
# day it is needed. pg_restore -l reads the archive's table of contents.
tables=$(docker compose -f "$compose" exec -T db pg_restore -l <"$file" | grep -c "TABLE DATA" || true)
[[ ${tables:-0} -gt 0 ]] || die "the dump has no table data in it; keeping $file for inspection"

size=$(wc -c <"$file" | tr -d ' ')
echo "backup: $file"
echo "backup: $tables tables, $((size / 1024 / 1024)) MB, verified readable"

# Old dumps are the operator's to keep or delete; saying how many there are is enough.
count=$(find "$out_dir" -name 'carcommand-*.dump' -type f | wc -l | tr -d ' ')
echo "backup: $count dumps in $out_dir"
echo "backup: sources kept in the app are sealed with CC_SECRET_KEY, which lives in .env and is not in this dump; keep .env safe beside it"

#!/usr/bin/env bash
# Put a dump back. This replaces what is in the database, so it asks first.
#
# Usage: scripts/restore.sh FILE [--force]
#
# The site is stopped for the duration. Restoring under a running site means the recorder
# writing rows into a half-restored schema, which is a worse problem than a minute of
# downtime.
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
file="${1:-}"
force="${2:-}"

die() {
    echo "restore: $*" >&2
    exit 1
}

[[ -n $file ]] || die "usage: scripts/restore.sh FILE [--force]"
[[ -f $file ]] || die "no such file: $file"
[[ -f $env_file ]] || die "no .env beside the compose file"

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

# Read the archive before touching anything. A corrupt file should fail here, with the
# database still intact, rather than half way through replacing it.
tables=$(docker compose -f "$compose" exec -T db pg_restore -l <"$file" | grep -c "TABLE DATA" || true)
[[ ${tables:-0} -gt 0 ]] || die "$file is not a readable dump with table data in it"
echo "restore: $file holds $tables tables"

# Is there anything at all in here? Counting drives alone was not enough: an install with
# five hundred named places, a service history and no drives read as empty and would have
# been replaced without a word.
#
# EXISTS rather than COUNT, so this does not walk a million positions to learn that the
# first one is there. Named tables rather than a loop over pg_tables: the clever version
# used query_to_xml, which returns an empty document for an empty table and makes xpath
# fail, and the error was being swallowed — so the check answered "empty" for every
# database, including full ones.
probe="SELECT (EXISTS(SELECT 1 FROM trips) OR EXISTS(SELECT 1 FROM charging_sessions)
             OR EXISTS(SELECT 1 FROM cc_places) OR EXISTS(SELECT 1 FROM cc_service)
             OR EXISTS(SELECT 1 FROM vehicles) OR EXISTS(SELECT 1 FROM cc_settings)
             OR EXISTS(SELECT 1 FROM cc_auth) OR EXISTS(SELECT 1 FROM cc_positions))"

if ! not_empty=$(docker compose -f "$compose" exec -T db psql -U "$db_user" -d "$db_name" -At -c "$probe" 2>&1); then
    # A check that cannot run must not wave the destructive thing through. Fail closed.
    echo "restore: could not tell whether this database has anything in it:" >&2
    echo "restore:   $not_empty" >&2
    [[ $force == "--force" ]] || die "refusing to restore blind — re-run with --force if you are sure"
    not_empty=t
fi

if [[ $not_empty == "t" && $force != "--force" ]]; then
    summary=$(docker compose -f "$compose" exec -T db psql -U "$db_user" -d "$db_name" -At -F' ' -c "
        SELECT (SELECT COUNT(*) FROM trips), (SELECT COUNT(*) FROM charging_sessions),
               (SELECT COUNT(*) FROM cc_places), (SELECT COUNT(*) FROM cc_service)" 2>/dev/null || echo "? ? ? ?")
    read -r n_trips n_charges n_places n_service <<<"$summary"
    cat >&2 <<EOF
restore: this database is not empty. It holds
restore:   $n_trips drives, $n_charges charges, $n_places named places, $n_service service records
restore: and a password, settings and rates besides. Restoring replaces all of it.
restore:
restore: Take a backup of what is there first:
restore:     scripts/backup.sh
restore: then run this again with --force.
EOF
    exit 1
fi

echo "restore: stopping the site"
docker compose -f "$compose" stop web >/dev/null 2>&1 || true

# --clean --if-exists drops each object before recreating it, so a restore over a
# populated database replaces rather than collides. Errors are not ignored: a restore
# that half worked is the worst outcome, and the operator needs to know.
echo "restore: restoring, this replaces the current contents"
docker compose -f "$compose" exec -T db pg_restore -U "$db_user" -d "$db_name" \
    --clean --if-exists --no-owner --no-privileges --exit-on-error <"$file"

echo "restore: starting the site"
docker compose -f "$compose" up -d web >/dev/null

after=$(docker compose -f "$compose" exec -T db psql -U "$db_user" -d "$db_name" -At \
    -c "SELECT COALESCE((SELECT COUNT(*) FROM trips), 0) + COALESCE((SELECT COUNT(*) FROM charging_sessions), 0)" 2>/dev/null || echo 0)
echo "restore: done — $after drives and charges in the database"

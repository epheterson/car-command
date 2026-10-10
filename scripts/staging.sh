#!/usr/bin/env bash
# A second stack on the same host, holding a copy of production's data, for trying a
# deploy on before the real one.
#
# Twice now a deploy has taken the live site down, both times the same way: a schema
# change or a backfill takes a lock, every reader queues behind it, and the site stops
# answering. Neither would have survived a dry run against real data — the first would
# have queued the same way, the second would have shown a six-minute backfill. Nothing
# about the code was wrong; the missing thing was somewhere to find out.
#
#   scripts/staging.sh up [DUMP]   bring it up and restore a dump into it (default: newest)
#   scripts/staging.sh check       every page, and how long the slowest took
#   scripts/staging.sh logs        the site's log
#   scripts/staging.sh down        remove it, volume and all
#
# It is a throwaway. Its volume, containers, network and port are all keyed on its own
# project name, so it cannot touch production's.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
if [[ -f $here/stack/docker-compose.yml ]]; then
    compose="$here/stack/docker-compose.yml"
    prod_env="$here/stack/.env"
else
    compose="$here/docker-compose.yml"
    prod_env="$here/.env"
fi

PROJECT="${CC_STAGING_PROJECT:-carcommand-staging}"
PORT="${CC_STAGING_PORT:-3055}"
SUBNET="${CC_STAGING_SUBNET:-192.168.63.0/24}"
TESLA_SUBNET="${CC_STAGING_TESLA_SUBNET:-192.168.65.0/24}"
BACKUPS="${CC_BACKUP_DIR:-$HOME/backups/carcommand}"
env_file="$here/.staging.env"

die() {
    echo "staging: $*" >&2
    exit 1
}

dc() {
    docker compose -f "$compose" --project-name "$PROJECT" --env-file "$env_file" "$@"
}

# Docker before anything that needs it, and before any of the work below leaves a
# half-made environment behind. A non-interactive shell often has a shorter PATH than the
# terminal the command was written in, which is how the sibling script came to report
# "the db container is not running" about a database that was running perfectly well.
command -v docker >/dev/null || die "docker is not on PATH (a non-interactive shell often has a shorter one than your terminal)"
docker compose version >/dev/null 2>&1 || die "docker is installed but 'docker compose' does not run — check that the daemon is up and the compose plugin is present"

make_env() {
    # Its own credentials, so a leak of these is not a leak of production's, and its own
    # project name so every docker object it creates is separate.
    [[ -f $prod_env ]] || die "no production .env to copy the shape of"
    if [[ ! -f $env_file ]]; then
        command -v openssl >/dev/null || die "openssl is needed to generate passwords"
        while IFS= read -r line; do
            case "$line" in
            DB_PASSWORD=* | WEBUI_PASSWORD=* | REST_PASSWORD=* | CC_SECRET=*) printf '%s=%s\n' "${line%%=*}" "$(openssl rand -hex 24)" ;;
            CC_PORT=* | CC_SUBNET=* | CC_TESLA_SUBNET=* | COMPOSE_PROJECT_NAME=*) ;;
            # No live car and no alerting from a copy: it would poll the same vehicle and
            # send the same messages to the same channel, from data that is not real.
            TESLAFI_TOKENS=* | TESLA_STREAM_HOST=* | DISCORD_WEBHOOK_URL=* | TELEGRAM_BOT_TOKEN=* | TELEGRAM_CHAT_ID=*) printf '%s=\n' "${line%%=*}" ;;
            *) printf '%s\n' "$line" ;;
            esac
        done <"$prod_env" >"$env_file"
        chmod 600 "$env_file"
        printf 'COMPOSE_PROJECT_NAME=%s\nCC_PORT=%s\nCC_SUBNET=%s\nCC_TESLA_SUBNET=%s\nCC_AUTH=off\n' "$PROJECT" "$PORT" "$SUBNET" "$TESLA_SUBNET" >>"$env_file"
        echo "staging: wrote $env_file with its own passwords"
    fi
}

wait_for_site() {
    for _ in $(seq 1 60); do
        curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1 && return 0
        sleep 2
    done
    dc logs web | tail -30
    die "the staging site never answered on $PORT"
}

case "${1:-up}" in
up)
    make_env
    dump="${2:-$(ls -t "$BACKUPS"/carcommand-*.dump 2>/dev/null | head -1 || true)}"
    [[ -n $dump && -f $dump ]] || die "no dump to restore. Run scripts/backup.sh first, or pass one."
    echo "staging: project $PROJECT on port $PORT, from $(basename "$dump")"
    # A volume older than the env file was initialised with a password this file does not
    # have, and Postgres only reads the password on first init. Start over rather than
    # fail authentication against a database this script created itself.
    if docker volume inspect "${PROJECT}_pgdata" >/dev/null 2>&1 && [[ $(docker volume inspect "${PROJECT}_pgdata" --format "{{.CreatedAt}}") < $(date -u -r "$env_file" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo 0) ]]; then
        echo "staging: its volume predates its credentials; replacing it"
        docker compose -f "$compose" --project-name "$PROJECT" down -v >/dev/null 2>&1 || true
        docker volume rm -f "${PROJECT}_pgdata" >/dev/null 2>&1 || true
    fi
    dc up -d --build db web
    wait_for_site
    echo "staging: restoring production's data into it"
    db_user=$(grep -E '^DB_USER=' "$env_file" | cut -d= -f2-)
    db_name=$(grep -E '^DB_NAME=' "$env_file" | cut -d= -f2-)
    dc stop web >/dev/null
    dc exec -T db pg_restore -U "$db_user" -d "$db_name" --clean --if-exists --no-owner --no-privileges --exit-on-error <"$dump"
    # pg_restore leaves the planner with no statistics, so the first queries pick bad
    # plans and time far worse than production ever would. Without this the numbers below
    # are not a dry run of anything — they are a measurement of a database that has not
    # been looked at yet.
    echo "staging: analysing, so the timings mean something"
    dc exec -T db psql -U "$db_user" -d "$db_name" -q -c "ANALYZE" >/dev/null
    dc up -d web >/dev/null
    wait_for_site
    rows=$(dc exec -T db psql -U "$db_user" -d "$db_name" -At -c "SELECT COUNT(*) FROM trips" 2>/dev/null || echo "?")
    echo "staging: up on http://localhost:$PORT with $rows drives — the same shape and size as production"
    echo "staging: now run  scripts/staging.sh check"
    ;;
check)
    [[ -f $env_file ]] || die "not up yet"
    slowest=0
    slowest_path=""
    fail=0
    for p in / /drives /charges /timeline /calendar /stats /charging /places /map /battery /sleep /software /tags /service /settings/cars /settings/money /settings/energy /settings/alerts /settings/look /settings/maps /settings/data /settings/access /day /health /api/now /api/driveprint; do
        # The trailing newline matters: without it `read` hits EOF, returns non-zero,
        # and `set -e` kills the script mid-loop with nothing printed.
        read -r code secs < <(curl -sS -o /dev/null -w '%{http_code} %{time_total}\n' "http://localhost:$PORT$p")
        printf '  %-16s %s  %ss\n' "$p" "$code" "$secs"
        [[ $code == 200 ]] || fail=1
        # Slowest page, because the thing a dry run is for is noticing a page that got
        # slow on real volumes rather than on the handful of rows a test fixture has.
        if awk "BEGIN{exit !($secs > $slowest)}"; then
            slowest=$secs
            slowest_path=$p
        fi
    done
    echo "staging: slowest was $slowest_path at ${slowest}s"
    # Every JSON endpoint, for every car, not just the default one. /api/now answered 500
    # for one of three cars for weeks: only that car's payload carries the nested block
    # that the serialiser walked past, and nothing had ever asked a page for a car other
    # than the one it opens on. A 200 alone is not enough either — the body has to parse,
    # because a half-written body is what a serialiser failure looks like from outside.
    db_user=$(grep -E '^DB_USER=' "$env_file" | cut -d= -f2-)
    db_name=$(grep -E '^DB_NAME=' "$env_file" | cut -d= -f2-)
    vins=$(dc exec -T db psql -U "$db_user" -d "$db_name" -At -c "SELECT vin FROM vehicles" 2>/dev/null | tr -d '\r')
    body=$(mktemp)
    for v in all $vins; do
        for p in /api/now /api/drives /api/charges /api/driveprint; do
            read -r code secs < <(curl -sS -o "$body" -w '%{http_code} %{time_total}\n' "http://localhost:$PORT$p?vin=$v")
            ok=$(python3 -c 'import json,sys; json.load(open(sys.argv[1])); print("json")' "$body" 2>/dev/null || echo "NOT-JSON")
            printf '  %-16s %-8s %s  %ss  %s\n' "$p" "${v: -5}" "$code" "$secs" "$ok"
            [[ $code == 200 && $ok == json ]] || fail=1
        done
    done
    rm -f "$body"
    if dc logs web 2>&1 | grep -q Traceback; then
        echo "staging: there are tracebacks in the log:" >&2
        dc logs web 2>&1 | grep -A5 Traceback | tail -20 >&2
        fail=1
    fi
    [[ $fail == 0 ]] || die "staging is not clean; do not deploy this"
    echo 'staging: clean. As far as this can tell, make web is safe.'
    ;;
logs)
    dc logs "${2:-web}"
    ;;
down)
    # Deliberately does not need the env file. Without this, losing that file stranded a
    # volume initialised with a password nothing had any more, and the next `up` failed
    # authentication against its own database with no clue why.
    if [[ -f $env_file ]]; then
        dc down -v
    else
        docker compose -f "$compose" --project-name "$PROJECT" down -v 2>/dev/null || true
        docker volume rm -f "${PROJECT}_pgdata" >/dev/null 2>&1 || true
    fi
    rm -f "$env_file"
    echo "staging: gone, volume and all"
    ;;
*)
    die "usage: staging.sh [up [DUMP] | check | logs | down]"
    ;;
esac

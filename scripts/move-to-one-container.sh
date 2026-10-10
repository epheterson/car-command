#!/usr/bin/env bash
# Move a compose install into the one container: its database, its key, its token file.
#
# Usage: scripts/move-to-one-container.sh [IMAGE] [VOLUME]
#        (defaults: carcommand:local, built by `make one-container`; the volume carcommand)
#
# What it does, in order, and nothing else:
#   1. refuses unless VOLUME does not exist yet: the move fills a new volume, never an old one
#   2. stops the compose site and connector, so nothing is written after the dump
#   3. dumps the database with scripts/backup.sh, into backups/ like any backup
#   4. restores the dump into VOLUME through the image, in one transaction, with CC_SECRET_KEY
#      from stack/.env as that volume's key, so the sources sealed in the app still open
#   5. copies stack/secrets/lucid-token.json into the volume, if there is one
#   6. writes stack/secrets/one-container.env with the settings from stack/.env that the one
#      container reads (alert channels, Tesla app, TeslaFi tokens ...)
#   7. takes compose down (no -v: its database volume stays), so `restart: always` cannot
#      bring it back beside the one container after a reboot; then prints the docker run line
#
# If any step fails, it says exactly how to put compose back. Going back later is the same:
#   docker compose -f stack/docker-compose.yml up -d
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
compose="$here/stack/docker-compose.yml"
env_file="$here/stack/.env"
image="${1:-carcommand:local}"
volume="${2:-carcommand}"
stage=""
keyfile=""

die() {
    echo "move: $*" >&2
    exit 1
}

# On any failure: what changed so far, and how to undo it.
on_exit() {
    local code=$?
    [[ -n $keyfile ]] && rm -f "$keyfile"
    [[ $code -eq 0 || -z $stage ]] && return
    echo >&2
    echo "move: stopped part way. Your compose database volume is untouched. To go back:" >&2
    echo "    docker compose -f stack/docker-compose.yml up -d" >&2
    if [[ $stage == restoring || $stage == restored || $stage == down ]]; then
        echo "and remove the half-made volume before trying again:" >&2
        echo "    docker volume rm $volume" >&2
    fi
}
trap on_exit EXIT

[[ -f $env_file ]] || die "no stack/.env: this is for an install that compose runs from this checkout"
command -v docker >/dev/null || die "docker is not on PATH"
docker image inspect "$image" >/dev/null 2>&1 || die "no image $image here; build it with  make one-container  (or pass ghcr.io/epheterson/car-command)"
if docker volume inspect "$volume" >/dev/null 2>&1; then
    die "the volume $volume exists already. The move fills a new volume; name another (make one-container-move VOLUME=...) or remove that one if it is yours to lose"
fi
docker compose -f "$compose" ps --status running --services 2>/dev/null | grep -qx db \
    || die "the compose database is not running; start it with  docker compose -f stack/docker-compose.yml up -d db"

echo "move: stopping the compose site and connector (the database stays up for the dump)"
stage=stopped
docker compose -f "$compose" stop web cc >/dev/null 2>&1 || true

stamp=$(date -u +%Y%m%dT%H%M%SZ)
dir="$here/backups/move-$stamp"
"$here/scripts/backup.sh" "$dir"
dump=$(find "$dir" -name 'carcommand-*.dump' -type f | head -1)
[[ -n $dump ]] || die "the backup wrote no dump"

# The key travels in a 0600 file that lives only for this command, never on a command line.
umask 077
keyfile=$(mktemp)
grep -E '^CC_SECRET_KEY=.+' "$env_file" >"$keyfile" || echo "move: stack/.env has no CC_SECRET_KEY; the one container makes its own (no sources were sealed without one)"

echo "move: restoring into the new volume $volume"
stage=restoring
docker run --rm -i --env-file "$keyfile" -v "$volume:/data" "$image" restore <"$dump" || die "the restore did not finish"
stage=restored

token="$here/stack/secrets/lucid-token.json"
if [[ -f $token ]]; then
    echo "move: copying secrets/lucid-token.json"
    docker run --rm -i -v "$volume:/data" --entrypoint /bin/sh "$image" \
        -c 'umask 077 && cat >/data/connector/lucid-token.json && chown 10001:10001 /data/connector/lucid-token.json' <"$token"
fi

# The settings the one container reads from its environment, under the same names. The
# passwords, the database settings and the compose-only names stay behind: the container
# keeps its own.
settings="$here/stack/secrets/one-container.env"
mkdir -p "$here/stack/secrets"
grep -E '^[A-Z_]+=.+' "$env_file" \
    | grep -vE '^(DB_USER|DB_PASSWORD|DB_NAME|WEBUI_USER|WEBUI_PASSWORD|REST_USER|REST_PASSWORD|CC_SECRET_KEY|COMPOSE_PROJECT_NAME|CC_PORT|CC_SUBNET|CC_TESLA_SUBNET|CC_CONNECTORS|MAPS_URL|TESLA_STREAM_HOST|TESLA_STREAM_ZMQ)=' \
    >"$settings" || true
names=$(cut -d= -f1 "$settings" | tr '\n' ' ')

custom=$(grep -E '^CC_CONNECTORS=.+' "$env_file" || true)
compose_only=$(grep -oE '^(MAPS_URL|TESLA_STREAM_HOST)=.+' "$env_file" | cut -d= -f1 | tr '\n' ' ' || true)

echo "move: taking compose down (its database volume stays)"
stage=down
docker compose -f "$compose" down
stage=""

cat <<EOF

move: done. The data is in the volume $volume; the dump stays in ${dump#"$here"/}.

Start the one container:

    docker run -d --name carcommand --restart unless-stopped --stop-timeout 30 -p 3044:3044 \\
        --env-file stack/secrets/one-container.env -v $volume:/data $image

Settings carried over in stack/secrets/one-container.env: ${names:-none}
Sign in with the same password. To go back instead: docker stop carcommand, then
docker compose -f stack/docker-compose.yml up -d. Compose's database volume goes only when
you remove it yourself (docker compose -f stack/docker-compose.yml down -v).
EOF
if [[ -n $custom ]]; then
    echo
    echo "Note: stack/.env sets CC_CONNECTORS. Build the image with the same connectors:"
    echo "    make one-container CC_CONNECTORS='...'   (the value from stack/.env)"
fi
if [[ -n $compose_only ]]; then
    echo
    echo "Note: ${compose_only}not carried over. The local maps add-on and the Tesla stream run only under compose for now."
fi

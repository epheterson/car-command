#!/usr/bin/env bash
# Tesla's live stream in the one container, as far as CI can take it without a tailnet:
# a node that has signed in before (a state file stands in for one) turns the stream on, a
# certificate stands in for the one net.sh gets from Tailscale, and then the receiver must
# run as its own user on 127.0.0.1 only, serve that certificate, be restarted on a new one,
# hand its socket to the site alone, and the site must be told the stream's name.
#
# Usage (on the runner, the container named carcommand already up): one-container-stream.sh PORT
set -euo pipefail
port=$1
name=carcommand-stream.tail0000.ts.net
ex() { docker exec carcommand "$@"; }
as() { local uid=$1; shift; docker exec carcommand setpriv --reuid="$uid" --regid="$uid" --clear-groups -- "$@"; }
fail() { echo "::error::$*"; docker exec carcommand carcommand status || true; exit 1; }
wait_for() { # seconds, then a command that succeeds once the wait is over
    local until=$(($(date +%s) + $1)); shift
    until "$@" >/dev/null 2>&1; do
        [ "$(date +%s)" -lt "$until" ] || return 1
        sleep 1
    done
}
site_up() { curl -fsS "http://localhost:$port/health"; }
receiver_running() { ex carcommand status | grep -qE '^receiver +running'; }
cert() { # a self-signed stand-in, as net.sh writes it: the name, the key, then the certificate
    local dir
    dir=$(mktemp -d)
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 \
        -subj "/CN=$name" -addext "subjectAltName=DNS:$name" -keyout "$dir/key" -out "$dir/crt" 2>/dev/null
    docker cp "$dir/key" carcommand:/tmp/new.key >/dev/null 2>&1
    docker cp "$dir/crt" carcommand:/tmp/new.crt >/dev/null 2>&1
    ex sh -c "printf '%s\n' $name >/data/tesla/certs/host \
        && install -o 10003 -g 10004 -m 0440 /tmp/new.key /data/tesla/certs/tls.key \
        && install -o 10003 -g 10004 -m 0444 /tmp/new.crt /data/tesla/certs/tls.crt \
        && rm /tmp/new.key /tmp/new.crt"
    openssl x509 -in "$dir/crt" -noout -fingerprint -sha256
    rm -r "$dir"
}
served() { # the fingerprint of what the receiver presents
    ex python3 -c 'import hashlib, ssl; print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate(("127.0.0.1", 4443)))).hexdigest())'
}
serves() { [ "$(served 2>/dev/null)" = "$1" ]; }
fp() { echo "$1" | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f'; }

echo "-- a node that signed in before turns the stream on"
ex sh -c 'mkdir -p /data/tesla/net && echo "{}" >/data/tesla/net/tailscaled.state'
docker restart carcommand >/dev/null
wait_for 180 site_up || fail "the site did not come back after the restart"
ex carcommand status | grep -qE '^receiver +waiting +for its certificate' || fail "the receiver did not wait for its certificate"
[ "$(ex stat -c '%a %U %G' /data/tesla/net)" = "700 tsnet tsnet" ] || fail "the node's state is not the node's alone"
[ "$(ex stat -c '%a %U %G' /data/tesla/certs)" = "2750 tsnet telemetry" ] || fail "the certificate directory is not set up for the receiver's group"
[ "$(ex stat -c '%U' /data/tesla/net/tailscaled.state)" = "tsnet" ] || fail "a state file put there by root was not handed to the node"

echo "-- a certificate starts the receiver, and the site learns the name"
first=$(fp "$(cert)")
wait_for 30 receiver_running || fail "the receiver did not start once it had a certificate"
# As the site's own user: a root shell in the container may not read another user's environ
# where the host kernel restricts it (Docker Desktop does).
site_env() { docker exec -u 10002 carcommand sh -c "tr '\0' '\n' </proc/\$(pgrep -u 10002 -f 'uvicorn app:server' | head -1)/environ"; }
site_knows() { site_env | grep -qx "TESLA_STREAM_HOST=$name"; }
wait_for 60 site_knows || fail "the site was not given the stream's name"
site_env | grep -qx 'TESLA_STREAM_ZMQ=ipc:///run/tesla-stream/zmq.sock' || fail "the site was not pointed at the receiver's socket"
wait_for 60 site_up || fail "the site did not come back after learning the stream's name"
web=$(ex pgrep -u 10002 -f 'uvicorn app:server' | head -1)
wait_for 15 serves "$first" || fail "the receiver does not serve the certificate it was given"

echo "-- the receiver: its own user, loopback only, its socket for the site alone"
rec=$(ex pgrep -f '^/usr/local/bin/fleet-telemetry' | head -1)
[ "$(ex stat -c %u /proc/"$rec")" = "10004" ] || fail "the receiver does not run as telemetry"
ex grep -q ' 0100007F:115B 00000000:0000 0A ' /proc/net/tcp || fail "the receiver is not listening on 127.0.0.1:4443"
if ex grep -q ' 00000000:115B 00000000:0000 0A ' /proc/net/tcp; then fail "the receiver listens on every address"; fi
wait_for 15 ex test -S /run/tesla-stream/zmq.sock || fail "no socket from the receiver"
[ "$(ex stat -c '%a %U %G' /run/tesla-stream/zmq.sock)" = "770 telemetry web" ] || fail "the receiver's socket is not the site's through the group: $(ex stat -c '%a %U %G' /run/tesla-stream/zmq.sock)"
connect='import socket; socket.socket(socket.AF_UNIX).connect("/run/tesla-stream/zmq.sock")'
as 10002 python3 -c "$connect" || fail "the site cannot reach the receiver's socket"
for uid in 10001 10003; do
    if as "$uid" python3 -c "$connect" 2>/dev/null; then fail "uid $uid reaches the receiver's socket"; fi
done
for uid in 10003 10004; do
    for target in /data/secrets/CC_SECRET_KEY /data/postgres/PG_VERSION /proc/1/environ /proc/$web/environ /data/logs/web.log; do
        if as "$uid" cat "$target" >/dev/null 2>&1; then fail "uid $uid can read $target"; fi
    done
    if as "$uid" ls /data/connector >/dev/null 2>&1; then fail "uid $uid can list the connector's files"; fi
done
if as 10004 cat /data/tesla/net/tailscaled.state >/dev/null 2>&1; then fail "the receiver can read the node's state"; fi
if as 10001 cat /data/tesla/certs/tls.key >/dev/null 2>&1; then fail "the connector's user can read the receiver's key"; fi
echo "the receiver runs as telemetry on 127.0.0.1:4443; only the site opens its socket; neither stream user reads the key, the database, the logs or another's environment"

echo "-- a renewed certificate restarts the receiver on it"
second=$(fp "$(cert)")
wait_for 30 serves "$second" || fail "the receiver still serves the old certificate"
echo "the receiver serves the renewed certificate"
ex carcommand status

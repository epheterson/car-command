#!/bin/sh
# The Funnel node the cars reach: tailscaled and the Funnel (serve.json) come up through the
# image's own start, containerboot. This runs beside it and keeps the receiver's certificate
# for the node's public name current in /certs, checking twice a day.
#
# The receiver reads its certificate once, when it starts. It shares this container's
# process space (pid: service:tesla-net in the compose file), so when a renewed certificate
# lands this ends the receiver and Docker starts it again on the new one.
#
# The one container (stack/aio) runs this too, as the node's own user, with TS_CERT_DIR and
# an empty RECEIVER_UID: there the directory's group lets the receiver read the key, and the
# supervisor restarts the receiver on a new certificate.
set -u
SOCK=${TS_SOCKET:-/tmp/tailscaled.sock}
CERTS=${TS_CERT_DIR:-/certs}
RECEIVER_UID=${RECEIVER_UID-65532} # the receiver image runs as this user, and it must read the key

/usr/local/bin/containerboot &
boot=$!
trap 'kill -TERM "$boot" 2>/dev/null; wait "$boot"; exit 0' TERM INT

ts() { tailscale --socket="$SOCK" "$@"; }

# The name Let's Encrypt can issue for: the tailnet lists it only once HTTPS certificates are on.
name() {
    ts status --json --peers=false 2>/dev/null | tr -d ' \n\t' | sed -n 's/.*"CertDomains":\["\([^"]*\)".*/\1/p'
}

renew() {
    host=$(name)
    if [ -z "$host" ]; then
        echo "tesla-net: no certificate name yet (still signing in, or HTTPS certificates are off for the tailnet)"
        return 1
    fi
    rm -f "$CERTS/new.crt" "$CERTS/new.key"
    ts cert --cert-file "$CERTS/new.crt" --key-file "$CERTS/new.key" "$host" >/dev/null || return 1
    chmod 0444 "$CERTS/new.crt"
    if [ -n "$RECEIVER_UID" ]; then
        chown "$RECEIVER_UID:$RECEIVER_UID" "$CERTS/new.crt" "$CERTS/new.key"
        chmod 0400 "$CERTS/new.key"
    else
        chmod 0440 "$CERTS/new.key"
    fi
    # The name the cars stream to; the one container hands it to the site.
    [ "$(cat "$CERTS/host" 2>/dev/null)" = "$host" ] || printf '%s\n' "$host" >"$CERTS/host"
    if [ -s "$CERTS/tls.crt" ] && [ "$(sha256sum <"$CERTS/new.crt")" = "$(sha256sum <"$CERTS/tls.crt")" ]; then
        rm -f "$CERTS/new.crt" "$CERTS/new.key"
        return 0
    fi
    had=no
    [ -s "$CERTS/tls.crt" ] && had=yes
    mv "$CERTS/new.key" "$CERTS/tls.key"
    mv "$CERTS/new.crt" "$CERTS/tls.crt"
    echo "tesla-net: certificate for $host written"
    if [ "$had" = yes ] && [ -n "$RECEIVER_UID" ] && pkill -f /fleet-telemetry; then
        echo "tesla-net: receiver restarted on the new certificate"
    fi
    return 0
}

while kill -0 "$boot" 2>/dev/null; do
    if renew; then wait_s=43200; else wait_s=300; fi
    i=0
    while [ "$i" -lt "$wait_s" ] && kill -0 "$boot" 2>/dev/null; do
        sleep 5
        i=$((i + 5))
    done
done
wait "$boot"

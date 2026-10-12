#!/bin/sh
# Hand over to the supervisor (supervise.py), which renders the config, runs CarConnectivity
# as the unprivileged user and runs it again when its sources change.
#
# Before that, as root: the sealing key and the passwords move out of the environment into
# files only root can read, and the supervisor starts without them in its environment, so
# no other process here can read them from /proc. The supervisor reads them back from the
# files; CarConnectivity, pip and every plugin run as `carcommand` without them (privsep.py).
#
# With arguments (make lucid-login), it runs that command as the unprivileged user instead.
set -eu
if [ "$#" -gt 0 ]; then
    exec setpriv --reuid=10001 --regid=10001 --clear-groups -- "$@"
fi
RUN="${CC_RUN_DIR:-/run/carcommand}"
umask 077
mkdir -p "$RUN/secrets"
chmod 0711 "$RUN"
chmod 0700 "$RUN/secrets"
for name in CC_SECRET_KEY DB_PASSWORD WEBUI_PASSWORD REST_PASSWORD CC_CONNECTORS_JSON; do
    eval "value=\${$name:-}"
    rm -f "$RUN/secrets/$name"
    printf '%s' "$value" > "$RUN/secrets/$name"
    chmod 0400 "$RUN/secrets/$name"
done
unset value
exec env -u CC_SECRET_KEY -u DB_PASSWORD -u WEBUI_PASSWORD -u REST_PASSWORD -u CC_CONNECTORS_JSON \
    PYTHONNOUSERSITE=1 python3 -s "${CC_SUPERVISE:-/app/supervise.py}"

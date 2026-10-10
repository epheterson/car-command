#!/usr/bin/env bash
# Run as root inside the one container (docker cp it in): what CarConnectivity, as uid
# 10001, can and cannot see. The key and the passwords stay with root, the site and the
# connector's supervisor; the database plugin logs in as its own role, cc_plugin.
set -u
as_plugin() { setpriv --reuid=10001 --regid=10001 --clear-groups -- "$@"; }
cc=$(pgrep -u 10001 -f 'carconnectivity ' | head -1)
web=$(pgrep -u 10002 -f 'uvicorn app:server' | head -1)
sup=$(pgrep -u 0 -f 'supervise.py' | head -1)
[ -n "$cc" ] && [ -n "$web" ] && [ -n "$sup" ] || { echo "::error::CarConnectivity, the site or the connector's supervisor is not running"; exit 1; }
echo "carconnectivity pid $cc (uid $(stat -c %u /proc/$cc)), site $web, connector supervisor $sup"
names=$(as_plugin cat /proc/$cc/environ | tr '\0' '\n' | cut -d= -f1)
if echo "$names" | grep -qE '^(CC_SECRET_KEY|DB_PASSWORD|REST_PASSWORD|WEBUI_PASSWORD|PLUGIN_DB_PASSWORD)$'; then
    echo "::error::a secret is in CarConnectivity's environment"
    exit 1
fi
echo "CarConnectivity's environment: $(echo "$names" | tr '\n' ' ')"
for target in /data/secrets/CC_SECRET_KEY /run/carcommand/secrets/CC_SECRET_KEY /proc/1/environ /proc/$sup/environ /proc/$web/environ /data/postgres/PG_VERSION /data/logs/web.log; do
    if as_plugin cat "$target" >/dev/null 2>&1; then
        echo "::error::uid 10001 can read $target"
        exit 1
    fi
done
echo "uid 10001 reads none of: the key (both copies), the environments of PID 1, the connector's supervisor and the site, Postgres's data, the logs"
# The database: no way in for uid 10001 but cc_plugin's password, which only its config holds.
for role in cc_site cc_plugin postgres; do
    if as_plugin env -u PGPASSWORD psql -w -h /run/postgresql -U "$role" -d carcommand -c 'SELECT 1' >/dev/null 2>&1; then
        echo "::error::uid 10001 logs in over the socket as $role"
        exit 1
    fi
    if as_plugin env -u PGPASSWORD psql -w -h 127.0.0.1 -U "$role" -d carcommand -c 'SELECT 1' >/dev/null 2>&1; then
        echo "::error::uid 10001 logs in over TCP as $role without a password"
        exit 1
    fi
done
echo "uid 10001 cannot log in over the socket as cc_site, cc_plugin or postgres, nor over TCP without a password"
if ! env -u PGPASSWORD setpriv --reuid=999 --regid=999 --clear-groups -- psql -w -h /run/postgresql -U postgres -d carcommand -qAt -c 'SELECT 1' >/dev/null 2>&1; then
    echo "::error::the check itself is broken: postgres cannot use its own socket"
    exit 1
fi
grep -q '"db_url": "postgresql://cc_plugin:' /run/carcommand/carconnectivity.json || { echo "::error::the database plugin does not log in as cc_plugin"; exit 1; }
echo "the database plugin logs in as cc_plugin"

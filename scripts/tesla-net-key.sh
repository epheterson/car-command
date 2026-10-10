#!/usr/bin/env bash
# Keep the Tailscale auth key the tesla-net container signs in with: asked for without
# echoing, written to secrets/tailscale-authkey (0600), never shown, logged or put in .env.
# The key is needed once; after the node has signed in its state volume carries it.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
dir="$here/stack/secrets"
[[ -d $here/stack ]] || dir="$here/secrets" # on the host, stack/ is the install itself
mkdir -p "$dir"
chmod 700 "$dir"
[[ -t 0 ]] || { echo "tesla-net-key: run it from a terminal (make tesla-net-key uses ssh -t)" >&2; exit 1; }
printf 'Tailscale auth key (tagged tag:carcommand-tesla, pre-approved; it is not echoed): '
IFS= read -rs key
echo
key=${key//[[:space:]]/}
if [[ $key != tskey-auth-* ]]; then
    echo "tesla-net-key: that is not an auth key (they start tskey-auth-). Nothing written." >&2
    exit 1
fi
umask 077
tmp=$(mktemp "$dir/.tailscale-authkey.XXXXXX")
printf '%s' "$key" >"$tmp"
chmod 600 "$tmp"
mv "$tmp" "$dir/tailscale-authkey"
unset key
echo "Kept in $(basename "$dir")/tailscale-authkey (0600). Next: docker compose --profile tesla up -d tesla-net tesla-telemetry"

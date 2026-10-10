#!/usr/bin/env bash
# A row written before an upgrade has to still be there after it.
. "$(dirname "$0")/lib.sh"
NAME="${2:?usage: $0 PORT NAME}"

has_place "$NAME" || {
    echo "::error::'${NAME}' was written before the upgrade and is gone after it"
    exit 1
}
echo "'${NAME}' survived the upgrade"

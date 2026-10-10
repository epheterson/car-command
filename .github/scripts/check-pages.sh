#!/usr/bin/env bash
# Every page and every data endpoint answers. Empty is fine; 500 is not.
. "$(dirname "$0")/lib.sh"

PAGES="/ /drives /charges /timeline /calendar /stats /places /map /battery /sleep /software /tags /service /settings/cars /settings/money /settings/alerts /settings/look /settings/data /settings/access /day /health /api/now /api/drives /api/charges /drives.csv /charges.csv /api/driveprint"
fail=0
for p in $PAGES; do
    code=$(curl -sS -b "$JAR" -o /dev/null -w '%{http_code}' "$BASE$p")
    printf '  %-16s %s\n' "$p" "$code"
    [ "$code" = "200" ] || fail=1
done
[ "$fail" = "0" ] || {
    echo "::error::a page did not answer 200"
    exit 1
}
echo "every page answered 200"

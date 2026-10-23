#!/usr/bin/env bash
# A release in two steps, so nothing reaches main, the tag or the image before the public CI
# has passed on the exact commit that will ship.
#
#   scripts/release.sh check          everything that can be checked here: a clean tree, the version
#                                     and its dated CHANGELOG section, the formatters at the versions
#                                     CI pins, every test suite, and the public snapshot (secrets,
#                                     the maintainer's private words, no session links)
#   scripts/release.sh stage          check, then put the snapshot on the public repo as one commit on
#                                     top of its main, pushed to the branch rc/<version> (never main):
#                                     tests and install path run there; this waits for both
#   scripts/release.sh ship           main moves to the staged commit (a fast-forward, never a force);
#                                     the release workflow tags it, builds the image and drafts the
#                                     release, which the maintainer reads and publishes
#
# REPO (default epheterson/car-command) names the public repository.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-epheterson/car-command}"
WORK="${TMPDIR:-/tmp}/cc-release"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$HERE/stack/web/version.py")
say() { printf '\n== %s\n' "$*"; }
die() { printf 'release: %s\n' "$*" >&2; exit 1; }

check() {
    cd "$HERE"
    say "clean tree, version $VERSION"
    [ -z "$(git status --porcelain)" ] || die "the tree has changes; commit or drop them first"
    grep -Eq "^## \[$VERSION\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md || die "CHANGELOG.md has no dated section for $VERSION"

    say "formatters at the versions CI pins"
    pins=$(sed -n 's/.*pip install \(black==[^ ]*\) \(ruff==[^ ]*\).*/\1 \2/p' .github/workflows/test.yml | head -1)
    [ -n "$pins" ] || die "no black/ruff pins found in .github/workflows/test.yml"
    venv="$WORK/fmt-$(echo "$pins" | tr ' =' '-_')"
    [ -x "$venv/bin/black" ] || { python3 -m venv "$venv" && "$venv/bin/pip" install -q $pins; }
    "$venv/bin/black" --check -q . || die "black would reformat files: run $venv/bin/black ."
    "$venv/bin/ruff" check -q . || die "ruff finds problems: $venv/bin/ruff check . --fix"

    say "test suites"
    (cd stack/web && python3 -m pytest -q -p no:cacheprovider) | tail -1
    (cd stack/cc && python3 -m pytest -q -p no:cacheprovider) | tail -1
    (cd stack/aio && python3 -m pytest -q -p no:cacheprovider) | tail -1

    say "the public snapshot"
    rm -rf "$WORK/snapshot"
    bash scripts/publish-snapshot.sh "$WORK/snapshot" "$REPO" >/dev/null
    if git -C "$WORK/snapshot" log --format=%B | grep -qiE 'claude\.ai/code|Claude-Session'; then die "a session link in the snapshot's message"; fi
    echo "snapshot ok: $(git -C "$WORK/snapshot" ls-files | wc -l | tr -d ' ') files"
}

stage() {
    check
    say "staging $VERSION on $REPO as rc/$VERSION"
    rm -rf "$WORK/public"
    git clone -q "https://github.com/$REPO.git" "$WORK/public"
    cd "$WORK/public"
    git fetch -q "$WORK/snapshot" main
    tree=$(git rev-parse 'FETCH_HEAD^{tree}')
    [ "$tree" != "$(git rev-parse 'origin/main^{tree}')" ] || die "the public main already has this tree"
    an=$(git log -1 --format=%an FETCH_HEAD) ae=$(git log -1 --format=%ae FETCH_HEAD)
    sha=$(GIT_AUTHOR_NAME="$an" GIT_AUTHOR_EMAIL="$ae" GIT_COMMITTER_NAME="$an" GIT_COMMITTER_EMAIL="$ae" git commit-tree "$tree" -p origin/main -m "Car Command $VERSION")
    git push -q origin --delete "rc/$VERSION" 2>/dev/null || true  # a candidate from an earlier stage
    git push -q origin "$sha:refs/heads/rc/$VERSION"  # the candidate branch only, never main
    echo "$sha" > "$WORK/staged"
    wait_ci "$sha"
    echo "staged $sha; scripts/release.sh ship moves main to it"
}

wait_ci() {
    local sha=$1 left=180 r
    say "waiting for tests and install path on ${sha:0:7}"
    while [ $left -gt 0 ]; do
        r=$(gh run list -R "$REPO" -L 30 --json name,status,conclusion,headSha -q ".[] | select(.headSha==\"$sha\") | select(.name==\"tests\" or .name==\"install path\") | \"\(.name)=\(.status)/\(.conclusion)\"" | sort -u | tr '\n' ' ')
        if [ "$(echo "$r" | wc -w)" -ge 2 ] && ! echo "$r" | grep -qE 'in_progress|queued|pending|waiting'; then break; fi
        sleep 20; left=$((left - 1))
    done
    echo "$r"
    echo "$r" | grep -q 'tests=completed/success' && echo "$r" | grep -q 'install path=completed/success' || die "the public CI did not pass; see gh run list -R $REPO"
}

ship() {
    [ -s "$WORK/staged" ] || die "nothing staged: scripts/release.sh stage first"
    sha=$(cat "$WORK/staged")
    cd "$WORK/public"
    git fetch -q origin
    [ "$(git rev-parse origin/main)" = "$(git rev-parse "$sha^")" ] || die "the public main moved since staging; stage again"
    say "main -> ${sha:0:7}"
    git push -q origin "$sha:refs/heads/main"
    git push -q origin --delete "rc/$VERSION" || true
    echo "the release workflow tags v$VERSION, builds the image and drafts the release:"
    echo "  https://github.com/$REPO/releases"
}

case "${1:-}" in
    check) check ;;
    stage) stage ;;
    ship) ship ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac

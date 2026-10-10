#!/bin/sh
# Build the public repository as one squashed snapshot of this tree.
#
#   scripts/publish-snapshot.sh /path/to/new/dir [owner/repo]
#
# owner/repo is the public repository's path (default epheterson/car-command). The README,
# docs and GitHub templates say OWNER/REPO in this tree; the snapshot gets the real path,
# and so does the rate-averages download URL, which names the repository too.
#
# What stays behind: the private planning docs (PLAN.md, docs/status.md, docs/teslafi-*.md, docs/ideas-from-elsewhere.md,
# docs/plans/, docs/release-readiness.md, docs/tesla-plan.md) — those live in the vault —
# and every untracked file. What is checked: the secrets test runs on the result, so no
# real VIN, coordinate, host or password rides along. Nothing is pushed; that is Eric's.
set -eu
DEST="${1:?destination directory}"; REPO="${2:-epheterson/car-command}"
case "$REPO" in */*) ;; *) echo "owner/repo, like epheterson/car-command" >&2; exit 1 ;; esac
HERE="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$HERE/stack/web/version.py")
[ -e "$DEST" ] && { echo "$DEST exists; pick an empty path" >&2; exit 1; }
mkdir -p "$DEST"
git -C "$HERE" archive --format=tar HEAD | tar -x -C "$DEST"
cd "$DEST"
rm -rf PLAN.md docs/status.md docs/teslafi-*.md docs/ideas-from-elsewhere.md docs/plans docs/release-readiness.md docs/tesla-plan.md
# The project's own instructions file refers to those docs; the public one is the README.
rm -f CLAUDE.md
# The public path, everywhere a link or a badge names it. This script is left alone: it says OWNER/REPO to explain itself.
grep -rlI --exclude-dir=.git -e 'OWNER/REPO' -e 'epheterson/car-command' . | grep -v '^./scripts/publish-snapshot.sh$' | while read -r f; do
  sed -i.snapshot "s#OWNER/REPO#$REPO#g; s#epheterson/car-command#$REPO#g" "$f" && rm -f "$f.snapshot"
done
if grep -rlI --exclude-dir=.git 'OWNER/REPO' . | grep -v '^./scripts/publish-snapshot.sh$'; then echo "OWNER/REPO left in the files above" >&2; exit 1; fi
git init -q -b main
git add -A
# The secrets test, on the tree that will be public; it reads git's file list, so after the add.
( cd stack/web && python3 -m pytest -q -p no:cacheprovider -W ignore test_no_secrets.py 2>/dev/null | tail -1 | grep -q " passed" ) || { echo "the secrets test failed on the snapshot; not committing" >&2; exit 1; }
git -c user.name="Eric Pheterson" -c user.email="151483+epheterson@users.noreply.github.com" commit -q -m "Car Command $VERSION

Every drive, every charge, every car. On your own server."
# No tag here: pushing main runs release.yml, which tags this version, builds the image and drafts the release.
echo "snapshot of $REPO $VERSION at $DEST: $(git rev-list --count HEAD) commit, $(git ls-files | wc -l | tr -d ' ') files"
echo "to publish: cd $DEST && git remote add origin <new empty repo> && git push -u origin main"

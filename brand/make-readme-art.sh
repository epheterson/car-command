#!/bin/sh
# Rebuild the README's generated art from the sources beside it:
#   brand/mark-arrive-night.svg   the arrival animation in gold, for the README on a dark theme
#   brand/social-preview.png      the 1280 x 640 card GitHub shows when the repository is linked
#   docs/screenshots/thumbs/*.png the feature grid: each 1280 x 800 screenshot, halved
#
#   brand/make-readme-art.sh
#
# Needs ImageMagick (magick) and a Chromium or Chrome for the card; CHROME=/path/to/chrome
# picks one, otherwise Playwright's cached headless shell or Google Chrome is used.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# The mark is one colour: ink on paper, gold on night (brand/README.md).
sed 's/#1B1A21/#F0C24A/g' brand/mark-arrive.svg > brand/mark-arrive-night.svg

if [ -z "${CHROME:-}" ]; then
  for c in "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell \
           "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell \
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "$(command -v chromium 2>/dev/null || true)" "$(command -v google-chrome 2>/dev/null || true)"; do
    [ -x "$c" ] && CHROME="$c"
  done
fi
[ -n "${CHROME:-}" ] || { echo "no Chrome or Chromium found; set CHROME=" >&2; exit 1; }
"$CHROME" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1280,640 \
  --screenshot="$HERE/brand/social-preview.png" "file://$HERE/brand/social-preview.html" 2>/dev/null
magick brand/social-preview.png -strip brand/social-preview.png

# Feature grid: the screenshots are 1280 x 800 windows, halved, so every cell is the same shape.
mkdir -p docs/screenshots/thumbs
for name in timeline drive calendar stats charging map; do
  magick "docs/screenshots/$name.png" -resize 640x400 -strip "docs/screenshots/thumbs/$name.png"
done
echo "made brand/mark-arrive-night.svg, brand/social-preview.png and $(ls docs/screenshots/thumbs | wc -l | tr -d ' ') thumbnails"

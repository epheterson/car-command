# Car Command identity

The star is the garage: one leg folded flat into the road, one point raised level into the roof, and a car pulling in under it, all in one line.

| File | Use |
|---|---|
| `mark.svg` | The mark, a 2.6-unit stroke in a 69.6 × 46.0 box. 48 px wide and up. |
| `mark-outlined.svg` | The same, the stroke expanded to a filled shape, for tools that do not take strokes. |
| `lockup-horizontal.svg`, `lockup-horizontal-night.svg` | The mark with the name (Jost Medium, outlined), on light and on dark. |
| `icon.svg`, `icon-light.svg` | The app icon, the mark inside the maskable safe circle. |
| `favicon.svg` | The reduced glyph for 16 and 32 px: the star-garage on its road under its roof, the car left out. Road and roof sit on whole pixels. |
| `mark-arrive.svg` | The arrival (SMIL): the star folds into the garage and the car rolls in, ending on `mark.svg`. |

Colours: ink `#1B1A21` on paper `#F6F5F0`; gold `#F0C24A` on night `#141319`. The mark is one colour: ink on paper, gold on night.

## Generated

| File | From |
|---|---|
| `media/demo-light.gif`, `media/demo-dark.gif` | The README's loop: the demo car on a phone, Now, Timeline, a drive, Stats and Places. Animated GIF, which GitHub plays everywhere; the README picks the theme with `<picture>`. |
| `media/demo-light.mp4` | The same loop as H.264, for places where a GIF is too heavy. |
| `media/desktop-light.png`, `media/desktop-dark.png` | A drive on a laptop-sized window, for posts and the docs. |
| `media/now-driving-light.png`, `media/now-driving-dark.png` | Now on a phone while the demo car drives to Work: the banner at the top. |
| `media/now-phone-dark.png`, `social-preview.png` | The Now page on a phone, and the 1280 × 640 card built around it (`social-preview.html`), uploaded in the repository's settings. |

`demo-routes.py` fetched the roads the demo car's drives follow (`stack/web/demo_routes.json`), once; the generator never goes online. `make-demo-media.sh` makes all of them from the demo car: Postgres in a temporary directory, the site, `make demo`'s six weeks around Springfield, then Playwright records and ffmpeg encodes. It removes everything it started when it exits. It needs uv, ffmpeg (with libx264) and ImageMagick.

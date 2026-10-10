# Changelog

Newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-10

The first public release.

### The site

- Pages for every car: Now, Timeline, Drives and Charges (every reading under each chart), Day, Calendar with a year view, Stats, Battery, Sleep, Software, Places and Map.
- Each car's own kWh per rated mile and parked drain, with Sentry (Halo Secure on a Lucid) and with climate, worked out from its history, with the sample count beside each figure.
- Stats with one period control, averages over the car's own months, cars side by side, and a month in progress compared day for day.
- Each car's year (from the VIN), make, model and paint. The paint sets the car's colour on charts and maps.
- Service log with reminders, tire sets and tread, trip tags, hidden drives and charges, a plug-in reminder, and alerts to Discord, Telegram, e-mail or Pushover.
- CSV and JSON export with per-car API tokens.
- **Connect an app:** an app signs in through the site (authorization code with PKCE) and gets a status token that reads each car's state without its location; `/api/stream` sends each car's state as it changes, for widgets and live activities. See [docs/api.md](docs/api.md).
- Raw readings kept as they arrived; `make replay` rebuilds a TeslaFi car's drives and charges from its feed. An hourly data check per car reports readings that arrived but weren't logged.
- Sign-in turns on (and off) from Settings, with no restart and no `.env` edit. The connect page sends a household there and back.
- Sign in with Cloudflare Access: set `CF_ACCESS_TEAM`, tap once in Settings, and a verified Access token is the sign-in, with no Car Command password. Apps whose redirect the install lists skip the Allow page. `make signin-reset` is the way back in.
- Dark mode and a phone layout.

### Sources

- **Tesla, direct:** Connect Tesla walks through your developer app, its key on your domain, Sign in with Tesla and the key in each car. The live stream sends position, speed, power, charging and the navigation route about once a second to a receiver on your box, through Tailscale Funnel, with no port opened at home. It doesn't wake the car.
- **One writer per car:** one source writes a car's drives, charges and sleep; others record beside it. Connect Tesla compares the stream with the current writer over the last week and can switch between them, and back.
- **TeslaFi:** export import, and a live feed that polls once a minute and fills gaps from TeslaFi's history.
- **TeslaMate:** history imported once, or followed live from its database, read-only.
- **Lucid:** through the CarConnectivity Lucid connector, signed in once from the site; the password is not kept.
- **Other brands:** Audi, Cupra, SEAT, Škoda, Volkswagen and Tronity through CarConnectivity, added from Settings. Plugins come from a catalogue of exact releases with the sha256 of every wheel, install with `--require-hashes`, run as an unprivileged user with no key or passwords, and use a database role limited to their own tables.
- **Cars and sources** (Settings): a card per car and a row per account; each source is tested before it is kept and sealed with the install's key.

### Powerwall

- **The house beside the cars, from the Tesla account:** the account Connect Tesla signed in (with the new `energy_device_data` scope, asked for only by the Powerwall step) gives the Powerwall's live status every five minutes, kept as rows of five-minute samples. An optional backfill reads its history back to the install date, a day a request, with progress that updates in place, a stop and resume, a back-off when Tesla slows it, and the day count and an estimate of the cost shown first.
- **Set up in the Tesla flow:** Connect Tesla gets Powerwall steps after the cars (Powerwall, History), each returning to its own step, with a numbered line of what is next; the account's single site is used without asking, and Cars and sources shows the same line on the Tesla account's card.
- **Now** shows a Powerwall row: its level and where the energy is moving.
- **Where a home charge's energy came from:** a bar of sun, Powerwall and grid on each home charge and its page, with each part's kWh and cost (the sun free, the Powerwall's grid-charged part at the rate it was bought at, marked ≈). Stats adds the sun's share of home charging by month.
- `GET /api/energy`: the current reading and today's kWh, for read tokens only (a status token is refused: the house's power shows when nobody is home). See [docs/api.md](docs/api.md).

### Costs

- Home plans with periods, a month-by-hour schedule and dated versions, so old charges keep the price in force then.
- **Find my plan** (US): import a utility's plan, every version of it, from OpenEI's Utility Rate Database.
- A price per place: free, per kWh, per minute, the home plan or the regional average.
- Supercharger sessions priced at what Tesla billed, with a link to the invoice.
- Fast-charging rates for Superchargers and for other networks.
- Home charges no plan covers take the regional household average for their month (US EIA, Eurostat), marked `≈`.
- Stats prices the same miles in a petrol car at average fuel prices (EIA, EU Weekly Oil Bulletin).

### Places and maps

- Places found from where the cars stop; Home and Work suggested from where they sleep. A name you set is kept.
- Tap a place name anywhere to name it, edit its address, pick an icon, set its price, or merge it with a place of the same name.
- Places page: a map beside your places, and unnamed stops by visits. Merge places on the map, set a place's size, search icons.
- A stop a Tesla navigated to is offered the destination's name.
- **Local maps** (optional add-on): place and street names from OpenStreetMap data built on your box, downloading only the regions your cars drive in. Address fields autocomplete from it.
- With local maps built, drives with sparse readings (a Lucid, a TeslaFi history) are matched to roads in the background.
- Public map backgrounds, the public geocoder and the weather lookup are off on a new install.

### Running it

- **One container:** `docker run` with one volume runs Postgres, the site and the connector and generates its passwords and key on first start; amd64 and arm64 images on GitHub's registry. `make one-container-move` moves a compose install into it.
- Docker Compose for the local maps add-on and the Tesla stream; `make setup` writes the passwords. One household password, or your proxy's sign-in.
- **Admin API:** admin tokens drive setup under `/api/admin/` (Connect Tesla, Cars and sources, Places, Money) from the local network or tailnet only, with an audit log in Settings.
- `make demo` (`carcommand demo` in the one container): six weeks of a fictional car.
- Backups checked by reading them back, and a restore that refuses to overwrite data.
- The schema upgrades in place at startup; CI upgrades an install with data in it on every change.
- PolyForm Noncommercial License 1.0.0.

### Not yet

- **Local map backgrounds:** with public backgrounds off, maps draw routes and places on a plain background.

[0.1.0]: https://github.com/epheterson/car-command/releases/tag/v0.1.0

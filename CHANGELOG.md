# Changelog

Newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-22

Commands, the garage door, every detail and analysis page filled in, and history that adds up.

### Added
- **Car commands** in a Controls card under the car on its Now page: tiles for charging, climate and the locks show the car's level, cabin and outside temperatures and locks, and change them after a confirming tap (a Climate or Halo tile is one switch); the charge limit is five taps beneath. Tabs hold the rest: charging current, cabin temperature, defrost, seat heat, lights, horn, wake, and, when Settings › Commands allows them for a car, Sentry and scheduled charging (tier 2), and unlock, trunk, frunk, windows and keyless driving (Access, the password again each time), then the command log. A command the car accepted shows on the card at once, until its own report catches up. Off until Settings › Commands turns them on; tier 2 and 3 commands are announced on the alert channels. See [docs/commands.md](docs/commands.md).
- **The rest of the controls:** a Tesla's charge port, Keep, Dog and Camp, cabin overheat protection, bioweapon defense, every seat's heat, the front seats' ventilation, wheel heat, media and HomeLink; a Lucid's alarm, windows, headlights, Max A/C, seats, wheel, battery preconditioning, AC current and keyless driving. A car is offered only what it has. A page under Settings › Commands tries each Lucid command on a parked car and puts it back.
- **Tesla's signing proxy** (`tesla-http-proxy` from teslamotors/vehicle-command, pinned) runs in the one container whenever a Tesla app is set, and as the `commands` profile in compose. Connect Tesla asks for `vehicle_cmds`, and an older sign-in is asked to sign in again. **Lucid commands** (charging, limit, climate, locks, trunk, frunk, lights, horn, wake) through the CarConnectivity Lucid connector and its REST plugin; Car Command turns them on in the connector's config. **Control tokens**, which may also send commands: `POST /api/cars/{vin}/command` ([docs/api.md](docs/api.md#commands)).
- **Tesla's live stream in the one container:** the Tailscale node and the receiver now run inside it, each as its own user, once it has a Tailscale auth key; Compose is no longer needed for it. `make one-container-move` brings a Compose install's node along. See [docs/tesla-stream.md](docs/tesla-stream.md).
- **More from a Tesla's stream:** climate, defrost, preconditioning, the charge port and its latch, charge rate, time to full, a waiting software update, and a sleeping car's last lock, doors, windows and charge limit, kept until the car says otherwise.
- **Charging hold:** per car, a charge at home inside the expensive hours (06:00 to midnight unless set) is paused after 10 minutes and started again when they end; a night's charge already running when the hours begin is left to finish, and a charge you restart yourself is left alone.
- **Garage door from Home Assistant:** link Home Assistant with its own sign-in under Settings › Data › Integrations (or `HA_URL` and `HA_TOKEN` in `.env`), pick the door in Settings › Alerts, and a car coming home opens it. The approach is learned from the cars' own trips home and timed so the door is open just before the driveway; a driveway camera confirms the car is turning in, and a camera that cannot look opens nothing. Your own taps are timed against both.
- **Close behind the car:** the camera watches the car go out and closes the door once it has cleared the garage and the mouth of the drive; a person standing in the drive leaves it open. Off, Watch only (notes what it would have done) or Close, with a Comings and goings log of every arrival and departure's timings. **Garage left open:** an alert once a door has stood open ten minutes with no car home, and the door's state on `/api/stream`.
- **Alerts for every car, whichever source reads it:** one watcher a minute, a sent log of what became of each alert, low battery once and re-armed 5 points up, left unlocked or a door open for ten minutes, a finished charge with its kWh and cost, and a silent connector counted as down. Newer Lucid connectors add someone left inside, the alarm, the guard mode with no USB drive, a low key fob and the charge port door left open. Webhook alerts: `ALERT_WEBHOOK_URL` (and `ALERT_WEBHOOK_TOKEN`) posts each alert as JSON to any receiver, beside Discord, Telegram, e-mail and Pushover. Telegram and Discord are set up step by step.
- **Drive pages:** start, route and end maps side by side, the route coloured by speed with a time-at-speed bar, a marker on the map that follows the chart, Replay, climb and descent, two headline figures and a Details grid, previous and next. **Charge pages:** where it was, what the charge bought until the next one (miles, battery and rated range used, Wh/mi, cost a mile), power, adding, volts and amps as lines. **Parked stretches** have a page of their own, opened from every parked row: battery and range lost, Sentry and climate time, cabin and outside at both ends.
- **Fix a drive by hand:** merge a drive with the next when the stop was under half an hour, split one at a reading picked on the chart or in the readings table, leave a bad reading out of the route, speeds and climb. Each is one Undo; nothing is deleted, every figure follows, and a re-import keeps the edits.
- **Lost drives rebuilt:** `make rebuild-drives` (also run in the background) rebuilds a connector car's drives from its states and readings, `make charge-starts` fills a charge the plugin closed without a start, and `make refacts` works stale drive and charge figures out again, with a dry run.
- **Terrain** (Settings › Maps, off by default): elevation from a terrain model kept on the box, so a drive from a car with no height reading gets a profile and every car counts the same hill. Tiles of Terrain Tiles on AWS (SRTM, USGS 3DEP, EU-DEM and others), fetched by square name only, Amazon seeing which one-degree squares your cars have been to; read locally. Climb, descent, Records and the elevation strip prefer the ground when the model covers the drive, and fall back to the car's GPS.
- **The view in the link:** the Map's centre, zoom, layers and hidden cars, and a chart's switched-on lines, are kept in the address as they change (replaced, never pushed, so Back still leaves the page); a copied link opens the same view.
- **Satellite close-ups** (Settings › Maps, off by default, needs your own free ArcGIS API key): Esri World Imagery for the drive page's start and end maps and the parked and charge strips, with Esri's credit. The key is sealed; the browser sends it to Esri with the tiles of the area viewed.
- **Smooth live map:** on Now the car's marker glides between fixes and an arrow turns with its heading; it jumps across gaps, rests in a hidden tab and with reduced motion.
- **Tesla API use** (Settings › Commands): every Fleet API call counted by car and kind, this month against Tesla's allowance, twelve months as a strip, the last 100 calls; a budget that slows background polling near the limit, refuses background wakes, alerts once at 80 % and pauses at 100 % while commands still go.
- **Records** on Stats (longest, most battery, top speed, coldest, hottest, highest, lowest battery, fastest and biggest charge, best and worst Wh/mi), each the real drive or charge. **Places › Routes:** the routes driven most, both ways counted, with time, energy and efficiency, and the places arrived at most.
- **Battery:** range at 100 % over the car's whole life, by odometer or date, with its trend, years and updates marked, and the loss from its best, its first or a start you type; tire pressure every day since the car first reported it, against the outside temperature. **Software:** range at 100 % and outside temperature per version, and a year of installs.
- **Analysis:** efficiency compared with the period before, a year earlier, a tire set or a tag at the same temperatures; miles and Wh/mi by outside temperature as a chart; a Charging › Where table for each place with charges, energy, time, average session, cost and the saving against petrol.
- **Calendar and Map:** bar strips beside the month's and year's totals, each day's Wh/mi and cost, the month's battery line, and a heat layer of where the cars charge.
- **MCP server** (`/mcp`): ask Claude or any MCP client about your own cars, read-only. Twelve tools (status, drives, charges, stats, battery health, records, routes, places, software), place names by default, coordinates only on a token made with them, one car per token if you like. Settings › Sign-in and API makes the token and shows the config to paste.
- **Find my plan** (Settings › Money): read a bill's PDF for its plan, community-choice provider and prices, or pick the utility from the home's ZIP code (looked up on the box, from NREL's list, CC BY 4.0); a short list of plans, each priced on a year of your own home charging. A bill is read on the server and never kept.
- **Energy hours:** `/api/energy/hours` gives each hour's rate period and all-in price under the home plan; a typed-in plan's hours are compared daily with OpenEI's tariff of the same code, and a difference shows on Money and as a Data check alert.
- **Bring history in from the browser** (Settings › Cars and sources): upload one or more TeslaFi exports, or point at a TeslaMate database, and watch it load. It runs in the background, survives a reload, resumes after a restart, and loading the same or a wider export again replaces what it loaded instead of doubling it. A car another source already logs live is filled in only up to where that source began.
- **Alert channels in Settings › Alerts:** add, test and remove Discord, Telegram, webhook, e-mail and Pushover without touching `.env`; secrets are sealed like source tokens. A new channel starts with a short set (charging finished, the security alerts, system problems), each section has an on or off for every alert in it, and switches save as they are flipped. Channels in `.env` keep working and say so.
- **First run** asks for the time zone (from the browser), units and currency before the first look.
- **Rename a car** in Settings › Cars and sources; the source keeps its own name, and two cars with the same name are told apart.
- **The garage door on the Timeline:** a drive the door opened or closed for says Garage opened or Garage closed under it.
- **Software history from the car:** a Lucid's every installed version and its date, from Lucid's own records (connector 0.4.0), back to the factory.
- **After the first import,** one offer to turn on map backgrounds and place names, saying what each service would see; both stay off until accepted.
- **Routes have pages:** tap a route for both ends as places (an unnamed one shown close up, at street level, with Name it), a map of its drives, its figures and its drives; See all in Drives lands on exactly those.
- **Home is one place:** set it with This is home; a car on its way reads "driving home from Work".
- **Car on, parked:** time with the car on but going nowhere (sitting in it, trying keyless driving) is parked time with its own colour on the strips and Day, never a drive.
- **Connect Tesla in the browser:** the developer app's Client ID, secret and domain are entered on the page, sealed, with numbered steps for making the app; `.env` still works.

### Changed
- **The mark:** the star smaller and in true proportion, the car further back; the icons and the social card are drawn from it again. Headers are glass: the page scrolls on under the top bars, with a hairline once it has.
- **Now:** a car with Sentry or Halo Secure on shows a red dot (not while it drives), named in the tooltip; a parked car's line names the town it is in or where its navigation is going; the battery chart shares the strip's window with a 24 h / 7 d toggle; a car out of reach reads Offline with its last-seen time; the two maps are the car close up and the day's drives; a phone stacks them. The Powerwall stays on All cars, and the whole garage is called All cars on every page.
- Cars are listed by name everywhere, whatever their source. A driving banner says where the drive set out from.
- The Lucid connector is 0.3.1: the charge port door again. Now's Doors tile says when it is open with no cable in, and an alert goes when the car is locked or asleep that way. Now, Software and Battery show the newer connector's own kWh, time to full, guard-mode reason and waiting version.
- Charts draw sampled series (speed, power, height) as lines and held ones (battery) as steps; a series in the main one's unit shares its scale; the middle label of a scale is round.
- Drives and Charges lists draw 500 rows at most, the last day whole, with Show older for the rest; a live charge reads "Charging now", and top-ups under 1 kWh count apart from charges.
- Pages load faster: map scripts are deferred and loaded only where a map is drawn, the map's drive-print cells are rebuilt in the background every 45 minutes, and a file with its content hash in the address is cached for a year.
- An address no route matches gets the site's own page, not raw JSON.
- `make setup` makes the Lucid token file; a TeslaMate import defaults to Tesla.
- **Less on screen that cannot apply:** the garage door appears once Home Assistant is linked, its close-behind row once a driveway camera is set, Tesla-only alerts and Settings rows only with a Tesla that can use them, and a brand's sign-in says plainly when CarConnectivity is not running, with the command that starts it. Maps with backgrounds off say so, with the switch, instead of a blank box; an unnamed place reads as unnamed.
- **One clock:** the 12- or 24-hour setting and the date order hold on every page, chart, time box and alert.
- A car keeps its colour when another is added, and every page keeps the car (or All cars) you had selected.
- The map libraries ship with the site; no page loads a script from another host.
- The install starts the connector with the rest (`up -d`); an install of imported history alone can still run `up -d db web`.
- The API answers a missing token, a wrong method and a bad parameter with JSON naming what was wrong.
- **The garage closes as the car pulls out:** the camera's sighting closes it at once, without waiting for the car's own report; the car's signs of life (unlocked, woken, Ready) keep the camera watching while the door is open.
- **Charges lead with the rate:** "all off-peak" or "82% off-peak", with the sun only when there was some. Tire pressure moved from Battery to Service.
- **Now's strip tells the time:** midnight with the day's name, 6 AM, noon and 6 PM, and now; 24 h and 7 d switch in place.
- **Maps on a phone:** one finger scrolls the page, two move the map, on the small maps; Map and Places stay free.
- **Places follow the selected car,** naming zooms to street level and returns to the list; the phone's top bar names the section; Volkswagen's three connectors are one row; the time zone is a searchable list of cities; the Powerwall sits under its Tesla account; data checks say All good.
- New docs: [the Tesla app](docs/tesla-app.md), [troubleshooting](docs/troubleshooting.md), and a reverse proxy in [install.md](docs/install.md).

### Fixed
- **Sentry and climate history:** Aware counts as Sentry on and Idle as off, in the stream and the feed, and a sleeping car ends a Sentry or climate run, so no 25-hour climate run and no night's drain counted as Sentry's.
- **Drives that add up:** TeslaFi drives keep their first and last minutes, drives filled from TeslaFi's summaries keep their energy, a top speed comes from a Tesla's stream, and miles driven out of reach are said where they fell instead of counted as parked time or phantom drain.
- **Charges that add up:** a charge over midnight counts on the day it began; a split charge takes its end back; a live charge's energy follows the car's own meter across a pause; a streamed Tesla charge counts what reached the battery; a charge that added under 0.05 kWh in under five minutes is no longer recorded.
- **Data check:** the odometer and the drives are counted over the same stretch, so a drive across the week's edge no longer reads as miles outside any drive.
- **A blind TeslaFi feed:** where the feed answers for ten minutes or more with no odometer or position, a Tesla's own stream fills the drives and charges in that span; a drive TeslaFi renumbers mid-way stays one drive; a feed row still saying Charging after the car stopped no longer sends a second start and stop.
- Charge-start alerts leave the power out while it is still ramping up (under 0.5 kW). The first-run setup page is styled, and a second stack on one host keeps its own images.
- The hourly rebuild of a Tesla's history no longer eats the night it opens inside. Nights lost since the feed began writing come back with `make replay VIN=... FROM=<date>`. Building the local map index no longer stops at a region whose outline is empty.
- A streamed Tesla drive ends when the car is put in Park for more than a minute, so a drop-off is two drives, as in TeslaFi and all earlier history; a stop at a light in Drive is still one. Comparing the Tesla stream with the stored history pairs sessions by overlap: a drive split differently is one difference, not two.
- **Software:** a version's days count between the dates the page shows, a version that ran under a day says its minutes or hours, and installs in one sitting count as one update.
- The demo ends today; the Data page no longer fails without a live connector; a fresh install sends no empty daily summary; `/health` answers before setup; a restarting connector no longer shows a warning for the minute it takes; a malformed `TESLAFI_TOKENS` entry is refused instead of becoming a phantom car.
- Money: a plan's always-applied first prices read Since the start, and an unpriced charge says "Not priced" and points at places. A daily summary goes out for every car.

### Security
- **Sign-in:** a browser that has signed in before is not held by the global brake, so strangers can't lock the owner out; IPv6 addresses count as their /64; an alert goes when failed sign-ins from many addresses start holding back new browsers; sign-in alerts and the admin call log record an address the client can't choose.
- **Sessions:** signing out ends the session on the server too; a password change ends every other session, even with `CC_SECRET` pinned; **Sign out everywhere else** in Settings › Sign-in and API. An admin token is refused on a request that came in through Tailscale Funnel, and a form sent without a length is read only as far as the form limit.
- **Secrets:** `make setup` and the one container keep the session signing key (`CC_SECRET`) outside the database, so a dump alone can't mint a session (older installs keep theirs; install.md says how to move it); the site and the connector refuse to start on the example's `CHANGE_ME` passwords; `make backup` writes dumps readable by their owner only.

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
- `GET /api/energy/hours`: the house's kWh by clock hour for a range of days, to price it by time of use; same doors as `/api/energy`.

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

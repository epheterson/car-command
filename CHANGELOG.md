# Changelog

Newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [0.2.2] - 2026-11-06

The car's controls rebuilt around its state, Face ID for the commands that open or disarm a car, the house's Powerwall and garage on Now, drives recovered from no-coverage stretches, everything the Lucid connector 0.5.0 publishes, and history from Tessie, a mileage logbook, charging fees and Home Assistant over MQTT.

### Added
- **Controls you read before you tap:** the car card's tiles are short: the lock and climate say what the car is (Locked, On · 68°), the frunk, trunk, windows and charge tiles are titled by name with an icon that shows their state (a vented window, an open trunk, a struck plug), and under each, what a tap does. One-tap commands (flash, honk, wake, keyless driving, HomeLink) and a tabbed panel for charging, climate, seats, security, lights and media sit behind a slim arrow, the last row of the tiles, and open within the card. Values are set with steppers and sent a moment after the last tap; the command log is a compact list that says who sent each command.
- **Face ID for unlocking:** unlocking, opening the frunk or trunk, the windows, keyless driving, HomeLink and disarming the alarm can be confirmed with a passkey, one Face ID or Touch ID per command, with no second tap. A command PIN works on any address, the LAN one too. Add either in Settings › Access › Confirming commands; every change sends an alert, and `make passkeys-reset` clears them if the phone is lost. A Cloudflare Access sign-in no longer counts as a confirmation, and the PIN or password now covers two minutes, not five.
- **Owners, and power that asks first:** through Cloudflare Access, only the identities named in `CC_ACCESS_EMAILS` unlock and open the cars or change what the site may do; anyone else Access lets in still sends everyday commands. Turning commands on, raising a car's tier, minting a token and setting the garage to open now ask for Face ID, the PIN or the password first; turning things off never does.
- **Lock controls and settings:** until a passkey or PIN exists, Settings › Commands offers one beside the commands switch, on or off, linking to where it is added.
- **Every unlock is told:** every try at unlocking or opening a car, refused ones too, goes to every alert channel at high priority, and no switch mutes it. A car takes **+ Access** only once an alert channel exists.
- **The house on Now:** beside the cars, a Powerwall tile (its level, what it is doing, and solar, home and grid) and a garage tile (the door, open or closed, since when). The garage tile carries one switch over all of the garage's watching, opening and closing, for every car: off at once, on with Face ID when it opens or closes the door; it shows for owners only, once Settings › Alerts has the garage doing something. A household with one car sees both on its car's Now.
- **The Powerwall on your own network:** read through a pypowerwall proxy, a reading every 15 seconds and a row a minute, with Tesla's cloud asked only when the proxy goes quiet and for the nightly refill. Settings › Powerwall lists both sources in the order they are used; the proxy's address is set there (checked before it is kept), or with `POWERWALL_RELAY_URL`.
- **The garage opens as the car comes down the street:** with `DRIVEWAY_STREET` set to the stretch of street the car comes home along, the driveway camera starts the door when the car moving along it heads for the driveway, a few seconds before it turns in, and only from shortly before the car is expected, so a car that parked up the street earlier opens nothing. Closing behind a car leaving happens the moment the camera sees it out.
- **Control tokens with limits:** a control token sends everyday commands, or car settings too if chosen, never unlocking. Any token can expire in a week, a month, three months or a year; by default it never expires.
- **The phone's tab bar is yours:** Settings › Look picks its sections and their order.
- **Driving into no coverage:** a car whose last reading is old while it says it is driving reads **No signal since 9:56** on Now and the cars list, not "driving now", and no alert, driving timer or garage-door watch acts on that stale word. The drive still open when the signal goes ends at the last reading, not at now.
- **The miles driven out of coverage are a drive:** when the odometer on the far side of the silence is higher than where the last drive ended, the stretch is added as a drive marked **no signal** (time from the last reading to the first fresh one, distance from the odometer, energy from the battery's drop, a dashed straight line on the map). Lucid and Tesla; never over a real drive or a charge (a charge in the silence cuts it into a leg before and after), once only. A stretch far longer than its distance needs (over an hour, or under 10 mph) reads "sometime between 9:47 and 4:05 PM" and counts no driving time or speed. The automatic fill takes only silences that began after it went live. `make fill-gaps FROM=...` lists the past ones; `ARGS=--write` adds them.
- **Range at your recent efficiency:** beside the car's own range estimate on Now and Battery, the range the charge now gives at the energy a mile the car used over its last ~50 logged miles ("182 mi at your last 52 mi"). Worked out with the other vehicle facts, hidden under 20 miles of driving or without a usable pack capacity.
- **A mileage logbook:** a CSV with one row per drive (date, start and end time, car, tag, start and end odometer, distance, start and end place), for a business or tax record. Filter by dates, car and tag from Drives or Tags; addresses and per-tag totals only when ticked. In your distance unit.
- **Idle and session fees:** a Supercharger bill is split into charging, idle and congestion fees on the charge's page. For charges no bill covers, a place or a network (Settings › Money) can carry a fee per charge and an idle price per minute after a grace, measured from the charge finishing to the plug coming out; the fees are part of the cost in every total.
- **Import history from Tessie:** Settings › Cars and sources › Bring history in takes a Tessie token (or `make import-tessie`, with `TESSIE_TOKEN` in `stack/.env`) and copies each car's drives, charges, routes and charge curves, read-only, month by month. It fills only the time no other source holds, a rerun replaces a month without doubling it, and an interrupted run carries on where it stopped. See docs/sources.md.
- **Home Assistant over MQTT:** with `MQTT_URL` set, each car's state, battery, range, charging power and limit, plugged in, lock, inside temperature, odometer and last-updated are published retained to your broker with Home Assistant discovery, on change and every ten minutes, with an online/offline last will. Position only with `MQTT_LOCATION=1`. Settings › Data shows whether it is connected. See docs/install.md.
- **Scheduled departure for a Tesla:** set a departure time from the Charge tab (the car warms the cabin and battery for it, every day), or the same with off-peak charging that ends at that time, or switch it off. Tier 2, like scheduled charging. Cars on firmware 2024.26 and later get Tesla's current schedule commands (a preconditioning schedule, and for off-peak a charge schedule that finishes by then), sent to a place you pick (the car's own Home or Work if it says them, your Home and Work places, where the car is, any other place; its position is used for that one command and stored nowhere); older ones get the legacy pair. The Now card shows the car's own schedules, each with the name of the place it applies to, read-only, whether or not commands are on, read from the car after a schedule command and in the daily identity read, and from the Tesla stream until then (Start again on Connect Tesla once to add its schedule fields). Lucid: its API has no schedule read or write in python-lucidmotors or the connector yet, so none is offered.
- **Everything the Lucid connector 0.5.0 publishes that an owner can use:** each field is read the way the others are and is simply absent for an older connector.
- **Why a charge is slow:** the Charge tile says "charger limits to 16 A" when the wall unit allows less than the car is set to, "power reduced by the car" when it derates, "preconditioning the pack", or "waiting for its schedule" when plugged in and not charging. The range the pack is holding back until it warms up sits beside the range.
- **Alarm, faults and updates on the card:** the alarm sounding, a module fault the car flags, an update installing ("Installing 42%", also on Software) or failed, and a tire sensor fault show on the card's line of odd things, and each sends an alert (Settings › Alerts: Alarm going off, Car fault flagged, Software update waiting or failed).
- **Sentry events:** the Security pane of the controls lists the car's latest Sentry events: when, the threat level, the kind and how many clips it holds. Metadata only; no picture address is read or fetched.
- **Tires:** Service says when the car itself took the pressures, and flags a failed sensor. The pressure history is dated by that time, not by when it was asked.
- **Gear and mode:** a car in Park with the drive state reads as idling, not driving. The drive page names the drive mode(s) it was driven in (Comfort, Sport+, Winter), kept from now on.
- **Trim and paint** fill the car's line in Settings › Cars and on Now where nothing else names them.
- **Electrify America plan:** Charging shows the plan, its end date and the days left; an alert goes at 30 and 7 days before it ends (Settings › Alerts: Electrify America plan ending). While the plan is current and is a free one, a charge at Electrify America is priced at nothing: below a bill, a typed price and a price set on the place, above the fast-charging rate. Charges from before the site first saw the plan are not repriced.
- **Lucid's own charge records:** matched to the site's charges by car and start time in a background pass (never on a page load). Where the car has no meter, the record's energy replaces the power-profile estimate (if it is the size the profile says); a charger named Electrify America marks the charge as one.
- **Privacy mode:** a car that is out of reach with privacy mode on says "Privacy mode" instead of "Offline", and the Day page shows the hours it was in it.

### Changed
- **The controls panel grows the car's card:** the arrow under the tiles is a bare chevron, and the panel it opens is the rest of that card, not a box below it. The trunk and frunk are drawn from the logo's car.
- **Now finds its way back:** on one car's Now, Now goes to every car; from anywhere else it goes back to whichever Now was open last, every car or one, for the session. The Today line keeps up with the cars without a reload, busiest car first.
- **Cars and sources:** a car's name is its header, and the pencil beside it sets a nickname. A pack the car's own source reports is shown as it is, with nothing to edit.

### Fixed
- **Sentry on a Tesla read from TeslaFi:** Now and the Security tab showed no Sentry state for a car whose newest reading was its feed, though the car had said; they show it now.
- **A Tesla's schedule that reads "charges at" and nothing more:** newer firmware streams the next start as a time and date, not minutes after midnight; it reads as its local time.
- **A Tesla command that fails says why:** the signing proxy's own words now follow "Tesla's servers didn't take the command" on Now and in the log.
- **The Commands page showed its switch off while commands were on:** saving that page would have turned them off.
- **A Lucid's climate no longer bounces back on:** after Off, the car reports climate on for about two minutes while it winds down, and the card believed it; Off now holds until the car agrees, three minutes at most.
- **The garage looks again for a car that is late:** a camera watch that saw nothing ended the trip when the car came inside home's radius still driving (held at a light), and the door stayed shut. It now watches again, up to three times, until the car parks; a known stop on the way gets a longer first watch. Only a sighting starts the door.
- **A parked car half hidden by the one leaving** is no longer taken for it by the driveway camera.
- **Energy no longer halves when Tesla's allowance runs low:** the Powerwall's live reading slows from every five minutes to every ten or twenty as the month's free Tesla API allowance runs down, and each reading was still counted as five minutes, so the house's kWh and an hour's coverage read half. A reading now stands until the next one, and each night any thin day of the last week is read again from Tesla's five-minute history.
- **Every route has a page:** a route below the busiest 25, or one listed only under another period, answered "not found" when opened from a bookmark or after switching the period or the car. Every route with enough drives now opens, and a route that is no longer there (a merged place, another car's route) goes back to the Routes list.
- **The clock setting reaches the Now strip at once:** switching to the 24-hour clock left the strip's hours in 12-hour for about ten seconds while the live stream sent what it had drawn before the save.

- **The charging current starts from the car's own:** the stepper read 32 A when the car had not said, so one tap set a car charging at 80 A down to 33 A. It now starts from what the car reports, and reads "Not reported yet" until it does.
### Security
- **Plain http:** Settings › Sign-in and API says **Not encrypted**, the commands that open the car are sent over plain http only from the home network, and install.md has an HTTPS section (Tailscale Serve, Caddy, a Cloudflare tunnel).
- **Command limits:** each command that opens the car has its own hourly limit (unlock 3, keyless driving 2, the others 6, a car), with 10 an hour across all cars, so unlock, frunk and trunk no longer share three.
- pypdf 6.21.4, for advisories on malformed PDFs (bill uploads).
- **Disarming the alarm asks for you:** turning a Lucid's alarm off or silent, and silencing it while it sounds, are tier 3, confirmed with a passkey, the PIN or the password like unlocking, so no token or stolen session can quiet it.
- **Sign-in guesses sent all at once** are each counted before the password is checked, so they can't all slip past the same wait.

## [0.2.1] - 2026-10-23

Dependency updates.

### Changed
- **The site's libraries:** FastAPI 0.143 (Starlette 1.9), Uvicorn 0.54, psycopg 3.3 and psycopg-pool 3.3, pypdf 6.21.
- **The connector's REST plugin** carconnectivity-plugin-restapi 0.1 (the first release after the alphas), the one the Lucid commands go through.
- **Tailscale 1.104** for the Tesla stream, in Compose and in the one container; **Valhalla 3.9.1** for the local maps.
- **Release and CI actions** moved to their latest releases, still pinned by commit.

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

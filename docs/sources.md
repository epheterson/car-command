# Sources

Every source writes to the same tables, so one install can mix them: a Lucid through the connector beside two Teslas from TeslaFi, say.

Add sources on **Settings › Cars and sources**. Each is tested before it is kept, sealed with `CC_SECRET_KEY`, and shown with whether it is working. Values already in `stack/.env` (`TESLAFI_TOKENS`, `TESLAMATE_DB_URL`) still work, and the page moves them into the app with one click.

| Source | Live data | Drive route and power | Charge curve | History before you connect | Setup |
|---|---|---|---|---|---|
| Tesla, direct | about once a second with the [stream](tesla-stream.md) (Compose only for now); none without it | both | yes | import from TeslaFi or TeslaMate | a [Tesla developer app](tesla-app.md), its key on your domain, a tap per car |
| TeslaFi | each minute | both | yes | the export | a token per car |
| TeslaMate | as TeslaMate records | both | yes | its database | its database address |
| Lucid | each minute, every 15 s driving | route only | yes | none | e-mail and password, once |
| Other brands (CarConnectivity) | per connector | route | per connector | none | the brand and its sign-in |

## Lucid

Through [CarConnectivity-connector-lucid](https://github.com/epheterson/CarConnectivity-connector-lucid). Sign in once on Settings › Cars and sources: the connector keeps only the refresh token Lucid returns, sealed with `CC_SECRET_KEY`, and not the password. Without the site, `make lucid-login` writes the token to `stack/secrets/lucid-token.json`; `docker compose -f stack/docker-compose.yml up -d` starts the connector.

Speed, tires, cabin temperature, Halo Secure, plug state, pack health and the update flag all come through. Lucid rate-limits per account, so run one logger per account (the Lucid app alongside is fine).

## Tesla

Pick one route per car.

- **Direct.** **Settings › Cars and sources › Connect Tesla** walks through it: your own app at developer.tesla.com, its key published on your domain and registered with Tesla, Sign in with Tesla, and the key added to each car from your phone. [Making the Tesla app](tesla-app.md) covers each field. It needs the site on HTTPS at a hostname you control, the app's Client ID, secret and domain, entered on that page (no restart; `.env` still works), and the key's path open to the internet with no sign-in in front of it. Tesla bills its API per use and asks for a card; see developer.tesla.com for prices. Drives and charges come only from the [live stream](tesla-stream.md), which needs Docker Compose for now and one public address, with no port opened at home. Without the stream, this route gives the car list, what Tesla billed for each Supercharger session ([costs](costs.md)) and the Powerwall, but no drives or charges.
- **TeslaMate, live.** Add its Postgres address (or `TESLAMATE_DB_URL` in `stack/.env`). Car Command reads positions, drives, charges, sleeps and software updates: the newest position every minute, and a rolling window replayed every ten minutes. It writes nothing to TeslaMate. Its database must be reachable from this stack: publish its port, or join the two compose networks.
- **TeslaFi feed.** A TeslaFi API token per car (TeslaFi › Settings › Advanced › API). It polls once a minute and replays the last 36 hours every hour through the same code as the export.
- **History only.** Import a TeslaFi export or a TeslaMate database once; add live data later.

## Other brands

Through the [CarConnectivity connectors](https://github.com/tillsteinbach/CarConnectivity#connectors). **Settings › Cars and sources › Add a source › Other brand**, then pick the brand and give its sign-in:

| Brand | Connector, at the release offered | Asks for |
|---|---|---|
| Audi | [carconnectivity-connector-audi](https://github.com/acfischer42/CarConnectivity-connector-audi) 0.3.2 | myAudi e-mail and password, S-PIN optional |
| Cupra, SEAT | [carconnectivity-connector-seatcupra](https://github.com/tillsteinbach/CarConnectivity-connector-seatcupra) 0.6.4 | the app's e-mail and password, S-PIN optional |
| Škoda | [carconnectivity-connector-skoda](https://github.com/tillsteinbach/CarConnectivity-connector-skoda) 0.13.1 | an API key from the MyŠkoda app, and each car's VIN |
| Tronity (many brands) | [carconnectivity-connector-tronity](https://github.com/tillsteinbach/CarConnectivity-connector-tronity) 0.7.3 | client ID and secret |
| Volkswagen (Europe) | [carconnectivity-connector-volkswagen](https://github.com/tillsteinbach/CarConnectivity-connector-volkswagen) 0.10.6 | e-mail and password, S-PIN optional |
| Volkswagen (US and Canada) | [carconnectivity-connector-volkswagen-na](https://github.com/zackcornelius/CarConnectivity-connector-volkswagen-na) 0.2.0 | myVW e-mail and password, country |
| VW Group (EU Data Act) | [carconnectivity-connector-vw-eu-data-act](https://github.com/mikrohard/CarConnectivity-connector-vw-eu-data-act) 0.3.1 | Volkswagen ID e-mail and password, make |

The sign-in is sealed and handed to the connector container. If the image doesn't carry that brand, the connector installs its plugin onto the `cc_plugins` volume and restarts. The connector container has to be running (`docker compose -f stack/docker-compose.yml up -d cc`) and able to reach PyPI the first time.

How a plugin is checked:

- `stack/shared/connectors.json` lists each brand's package, its exact release, its connector type, and the sha256 of every wheel it adds. The connector refuses any request that doesn't match an entry, before pip runs.
- pip installs with `--user --require-hashes --no-deps --only-binary :all:`, with everything already installed held at its release. A wheel with a different hash is not installed. `make connector-lock` writes the hashes from inside the image and checks each against PyPI's.
- pip, the import check and CarConnectivity run as the unprivileged `carcommand` user, with no key or passwords in reach. Root reads those to write CarConnectivity's config, and root's Python never loads anything from the plugins volume.
- CarConnectivity logs in to Postgres as `cc_plugin`, a role limited to the plugin's own tables. It can't read the sealed sign-ins or Car Command's state, or create tables. Its password is new at each start.
- Each sealed sign-in names its own account row, so one copied into another row opens as nothing. One the key can't open is kept and tried again after a restart.

If a new brand stops CarConnectivity on its first start (usually a wrong password), it's taken out, its sign-in dropped and the reason shown; other cars keep running. If CarConnectivity keeps stopping within two minutes of starting, it starts without the other brands after the second time and retries each later. A plugin that won't install is retried with growing waits. Lucid and every brand already working start first. Two brands whose plugins need different releases of one library (Audi and Cupra want different PyJWT) can't share the volume; the second says so. Removing a brand removes its plugin too, unless you untick that.

The site reads state, battery, range, position, odometer and charging for every brand; tire, cabin and Halo Secure details are Lucid-only for now. Map lookups go out rounded to about 100 m, or not at all, per Settings › Maps. We haven't run these brands ourselves; if you do, [open an issue](https://github.com/epheterson/car-command/issues/new/choose). Volvo and Renault/Dacia connectors are early alphas, and Volvo's needs an older `requests`, so they aren't offered.

**Building a connector into the image:** name the package in `CC_CONNECTORS` and its config in `CC_CONNECTORS_JSON` in `stack/.env` ([examples](../stack/.env.example)), then `docker compose -f stack/docker-compose.yml build cc`. A brand added in Settings uses the image's copy when there is one; a block in `CC_CONNECTORS_JSON` wins over the same brand in Settings.

The site and the connector each ship a copy of the catalogue. `make web` and `make deploy` rebuild whichever runs a different copy (`scripts/catalogue-sync.sh`), and a brand saved at another release is moved to the pinned one, its sign-in kept.

The connector container publishes no port. Its config, sign-ins included, is readable only by it and root, and its logs mask passwords in addresses.

## Powerwall

The Powerwall is read from the Tesla account Car Command already has; nothing is read from the gateway on your network (Tesla's newer firmware removed its web login, and a Powerwall 3 never had one). It is set up inside Connect Tesla, as steps after the cars:

1. **Signed in.** The sign-in asks for the `energy_device_data` scope together with the vehicle ones (the Powerwall step's "Bring it in" does this). A sign-in from before this release lacks the scope, and Connect Tesla and Settings › Energy say so. The ordinary car sign-in never asks for it, so a developer app that does not allow Energy Product Data still signs in for cars; once the token holds it, or the Powerwall is on, a car re-sign-in keeps it.
2. **Your Powerwall.** The account's energy site is used on its own when it has exactly one; with several you pick. Its install date is kept.
3. **History.** Optional and once: Tesla's five-minute power history, one request a day from yesterday back to the install date, newest first and throttled to a request every couple of seconds. The step shows the day count and an estimate of the cost before you start (Tesla bills Fleet API requests to your developer account beyond a free allowance), a bar that updates in place while it runs, and Stop and Resume. A restart resumes it. When Tesla asks it to slow down it waits 60 seconds, doubling to 30 minutes. A day that already holds enough readings is skipped.

Every action returns to its own step, and the numbered line shows what is next. Cars and sources shows the same line on the Tesla account's card. Settings › Energy is where it is managed: status, turn off, stop and resume.

While a site is on, Tesla's `live_status` is read every five minutes: it is the Powerwall row on Now (fifteen minutes is as old as it shows) and a row of `cc_energy`. Every row there is one five-minute sample, held for the five minutes from its time. The table keeps a `source` column (`cloud` for all of them) so another source can come later; a local gateway was tried and removed, and what it left in the database is deleted at startup.

**Where each charge's energy came from.** Judged by what plugging the car in drew. Once a home charge has ended and its five-minute readings are in, each minute of it is taken against the house's reading for that minute:

- The house's own load (its reading less the car's draw) was there before the car, and solar serves it first.
- The car gets the sun that was left over, which would otherwise have gone to the Powerwall or the grid, and nothing more. A car drawing 7.5 kW beside a house drawing 2.8 kW under 3.3 kW of sun is 0.5 kW sun and 7 kW grid, not the house's 30% sun.
- What the car draws beyond that is shared by the Powerwall and the grid in the proportion they were giving.
- A minute is held to what the house drew, since the house reading is a five-minute average and the car's draw is one minute's; sun that was exported is not the car's.

The Powerwall's own part is then split by what went into it over the seven days before the charge: its solar share is the solar-charged energy over all that went in. Costs count the sun as free, price the grid's part at the home plan's rate for each minute, and price the Powerwall's grid-charged part at the weighted rate that energy went in at; that part is marked ≈ because it is a seven-day average. Five-minute readings are the resolution there is, so they are not marked as an estimate; a figure is ≈ only where the readings have gaps (under 95% of the charge covered) or the Powerwall has no history to read its mix from. A charge with under half of it covered shows no split. The result is stored beside the charge facts, so no page works anything out; it does not replace the charge's cost, which stays what the plan prices it at. When the way a split is worked out changes, every stored one is deleted once at startup and the recorder works them out again.

## Importing history

**In the browser.** Settings › Cars and sources › Bring history in.

- *TeslaFi:* choose one or more exports (`.csv` or `.csv.gz`, up to 1 GB each; `CC_IMPORT_MAX_MB` changes the limit), then Import. Each file is checked on arrival: a file that is not a TeslaFi export is turned away before anything starts. Uploads are kept on the data volume (`CC_IMPORT_DIR`, `/data/imports`) only while the import runs.
- *TeslaMate:* paste the database address (or use the saved one) and Import; every car in it comes in, read-only, up to where Car Command's own logging of that car began.
- The import runs in the site's background, one at a time, and shows the file it is on, its progress and, at the end, the drives, charges and days loaded and any file it skipped. Reload the page or come back later; it carries on. A restart interrupts it: the page offers Resume (TeslaFi) or Start over, and a TeslaMate import is started again from its form.
- Loading the same file again replaces what it loaded; nothing doubles. When it ends, the places in the history are named by whatever Settings › Maps allows.
- Behind a proxy that caps request size (Cloudflare's free plan allows 100 MB), upload from the LAN address.

The command line does the same, for a host without a browser or a file too large to upload.

**TeslaFi.** Export your history (TeslaFi › Settings › Account › Data Export, a `.csv.gz`), then load it:

```sh
make import FILE=path/to/TeslaFiData.csv.gz          # over ssh, on the host in Makefile.local
# or, on the machine running the stack:
docker compose -f stack/docker-compose.yml cp TeslaFiData.csv.gz web:/tmp/TeslaFiData.csv.gz
docker compose -f stack/docker-compose.yml exec -T web python3 teslafi_load.py /tmp/TeslaFiData.csv.gz
# or, in the one container:
docker cp TeslaFiData.csv.gz carcommand:/tmp/TeslaFiData.csv.gz
docker exec carcommand carcommand web python3 teslafi_load.py /tmp/TeslaFiData.csv.gz
```

Add `--dry-run` (with `make import`, `ARGS=--dry-run`) to read the file and print what it would load, without writing.

The loader streams the file and rebuilds drives, charges, positions, firmware history and state the way the live logger would. Loading the same file twice changes nothing. How the columns are read is at the top of `stack/web/teslafi_import.py`.

When a TeslaFi feed starts weeks after its last export, the first poll fills the gap from TeslaFi's drive and charge summaries (endpoints, odometer, levels, energy, max speed, temperature; no routes or curves). `make teslafi-history VIN=… FROM=… [TO=…]` (over ssh) does the same by hand; locally, `docker compose -f stack/docker-compose.yml exec -T web python3 teslafi_history.py VIN FROM [TO]`, with dates as `2026-09-04`.

**TeslaMate.** Dump six tables from TeslaMate's database as CSV (read-only), copy them in, and load them. It stops where Car Command's own logging of that car began, so the two never overlap. Find TeslaMate's database container with `docker ps`: its own compose file names it `teslamate-database-1`, with user and database `teslamate`.

```sh
mkdir tm
for t in positions drives charging_processes charges states updates; do
  docker exec teslamate-database-1 psql -U teslamate -d teslamate -At \
    -c "\copy (select * from $t) to stdout with csv header" > tm/$t.csv
done
# compose:
docker compose -f stack/docker-compose.yml cp tm web:/tmp/tm
docker compose -f stack/docker-compose.yml exec -T web python3 teslamate_load.py /tmp/tm \
  --vin <VIN> --name <name> --model "Model 3" --capacity 75
# the one container:
docker cp tm carcommand:/tmp/tm
docker exec carcommand carcommand web python3 teslamate_load.py /tmp/tm \
  --vin <VIN> --name <name> --model "Model 3" --capacity 75
```

- `--vin` and `--name`: the car's VIN and the name the site shows. Both are required.
- `--manufacturer`: the default is `Tesla`; name the make for a car logged through a bridge (for example `Lucid`).
- `--model`: free text, shown on the site. `--capacity`: the usable pack in kWh, used to work out a drive's energy from its charge levels; Settings can change it later. Both optional.
- `--dry-run`: read and count, write nothing. `--until 2026-09-04T05:06:29Z` cuts at a time of your choosing instead of where live logging began.
- The dump reads every car in TeslaMate as one. With several cars, dump one at a time: add `where car_id = N` to each table's select, and for `charges`, `where charging_process_id in (select id from charging_processes where car_id = N)`.

`make import-teslamate` (over ssh) does the same on the host in `Makefile.local`, with `TM_VIN`, `TM_NAME`, `TM_MODEL` and `TM_CAPACITY` set there (and `TM_MAKE` for a make other than Tesla); `TM_DB` (default `teslamate-db`) names TeslaMate's database container and `TM_DB_USER` (default `teslamate`) its user and database.

**Naming places.** `make discover` (over ssh; locally `docker compose -f stack/docker-compose.yml exec -T web python3 discover.py`, or `docker exec carcommand carcommand web python3 discover.py`) clusters trip and charge endpoints and names each cluster once: from the [local maps add-on](maps.md) when it runs, or the public Nominatim geocoder if you turned it on (one request a second, coordinates rounded to three decimals). A name you set on the Places page is not replaced.

## Raw readings

Every reading is kept as it arrived, so drives and charges can be worked out again after a fix (`make replay VIN=… FROM=…` over ssh for a TeslaFi feed, or locally `docker compose -f stack/docker-compose.yml exec -T web python3 teslafi_live.py VIN FROM`), and a day's readings are a page and a CSV. Once an hour per car, a data check looks for readings that arrived but weren't logged, odometer miles in no drive, a failed rebuild, or a quiet source. Findings show in the footer and on Settings › Data, and go out once a day as an alert.

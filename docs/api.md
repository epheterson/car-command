# API and exports

## Tokens

Mint a token on **Settings › Sign-in and API**. It is shown once and kept only as a hash. Send it as a header, not in the URL (query strings end up in access logs):

```sh
curl -H "Authorization: Bearer $TOKEN" http://your-host:3044/api/now
```

**Read tokens** open the `/api/` endpoints below and nothing else: no page, no form. **Control tokens** read what a read token reads and may also send the car commands (see Commands); no other kind ever can. **Status tokens** (made by connecting an app, or as a third kind in Settings) open `/api/now` and `/api/stream` only, without the car's location (see Connecting an app). **MCP tokens** open `/mcp` and nothing else, and `/mcp` opens for no other kind (see MCP). A token scoped to one car answers for that car only, and must name it with `?vin=`. Tokens minted before admin tokens existed are read tokens.

**Admin tokens** also drive setup under `/api/admin/`: the same actions as the buttons on Connect Tesla, Cars and sources, Places and Money, through the same code. An admin token covers every car. Only a bearer admin token opens `/api/admin/`; a browser session doesn't, so those routes need no cross-site form token.

Deleting a token on the same page stops it at once. `/api/whoami` says what a read or admin token is; a status token gets 403 there.

## Connecting an app

An app signs in through the browser and is handed a **status token**, with nothing to copy. It is the authorization-code flow with PKCE, and any client can use it. **Sign-in must be on**: with `CC_AUTH=off` the connect page refuses, because otherwise anyone on the network could mint a token. Behind Cloudflare Access the sheet shares the system's cookie jar, so Cloudflare's own sign-in lets it through to the consent page and the install needs no Car Command password (see [Behind Cloudflare Access](install.md#behind-cloudflare-access)). The refusal page has a button to Settings › Sign-in and API, where one password turns it on without a restart, and the person is brought back to the connect page afterwards.

1. The app makes a random `code_verifier` (43 to 128 characters of `A-Z a-z 0-9 - . _ ~`) and a random `state`, and opens `GET /connect` in a browser sheet (the platform's web-authentication sheet) with `client` (the app's name, 40 characters at most), `redirect_uri`, `state` (required, 256 characters at most), `code_challenge` (the unpadded base64url of the verifier's SHA-256) and `code_challenge_method=S256`. The `redirect_uri` must be the app's own callback, such as `example-app://connected` or `com.example.app:/callback`. Anything that could hand the code to a web page is refused (see the rule below), and so is an address with a fragment. A request with anything wrong shows an error page and never redirects.
2. The owner signs in if they are not already, and sees "client wants to read your cars" with Allow and Cancel, and under it the name the app gave itself and the whole address it will open. The name is the app's own claim, so the address is what to check.
3. Allow redirects (303) to `redirect_uri` with `code` and `state` added to any query it already had; Cancel redirects with `error=access_denied` and `state`. The app checks `state` matches before going on. A code works once, for five minutes.
4. The app posts the code to `POST /api/connect/token` with `{"code": "…", "code_verifier": "…", "device": "Example phone"}` (`device` is optional, 40 characters at most). Send the same `redirect_uri` too, which is recommended: when it is present it must equal the one the owner was sent to, or the answer is `invalid_grant`. The body may be 4 KB at most (`413 {"error": "too_large"}`). This path needs no session and no token. Success is `200 {"token": "…", "kind": "status"}`, listed in Settings under its name and device, where it can be revoked. Any failure is `400 {"error": "invalid_grant"}`, and a code is spent by the first attempt, right or wrong. Repeated failures from one address (an IPv6 network counts as one) are answered `429 {"error": "slow_down"}` with a `Retry-After` header.

A code is kept in the database, bound to nothing but its PKCE challenge and its expiry: not to a host, a cookie or a URL prefix. The owner can allow on one address (the internet, through a proxy) and the app can redeem on another (the home network).

**Trusted apps skip the Allow page.** An install that lists an app's redirect in `CC_APP_REDIRECTS` trusts that app, once its owner has allowed it by hand: the first connect of a listed app shows the Allow page and remembers the app (by redirect and name). After that, when the request is already signed in (a password session or Cloudflare Access) and `redirect_uri` is exactly a listed entry, `GET /connect` issues the code and redirects straight back, so the sign-in sheet just closes. Settings › Sign-in and API lists the remembered apps, with Forget to bring the Allow page back. An app is remembered only while the token its Allow produced exists: revoke that token and the next connect asks again, and a skipped connect moves the app to its newer token. The token is still a status token, still listed in Settings, and an alert still goes out. Unlisted redirects always see the Allow page, and nobody signed in is sent to sign in first.

**Which redirects are allowed.** Browsers and launchers have schemes that open a web address handed to them, and no blocklist of them is complete, so an unlisted redirect must have exactly one plain shape: a scheme, then optionally `//` and one lowercase label (letters, digits and hyphens, no dot, port or userinfo, and starting with a letter), then a path of segments of letters, digits, `_`, `~` and `-` (no dots). It has no query and no fragment, no percent-escapes and nothing but ASCII. `example-app://connected`, `example-app://auth/done`, `example-app:/callback` and `com.example.app:/callback` pass; the dots of a reverse-DNS scheme are fine. A scheme that is `intent`, contains `http`, `chrome`, `safari`, `firefox`, `edge`, `opera`, `brave`, `browser` or `search`, or is `javascript`, `data`, `file`, `blob` or another web or system scheme is refused whatever the rest looks like. **Any install that knows its apps should set `CC_APP_REDIRECTS`** in `stack/.env` to a comma-separated list of exact redirect addresses. When it is set, only an exact match passes (no prefixes, no added path or query), and a match may have a query or a host, which is how an app that needs more than the plain shape connects. It never admits `http` or `https` or the refused schemes.

**Status tokens.** A status token opens `GET /api/now` and `GET /api/stream` and nothing else (every other path is 403). Its answers have these keys removed, wherever they appear: `lat`, `lon`, `address`, `place`, `place_named`, `heading`, `speed_mph`, `at_home`, `odometer_mi` and `meters` (`place` and `at_home` come back only with the grant below) (two readings of either give the car's speed). `vin` stays. Read tokens, minted in Settings, see everything the endpoints below return.

**Sharing the place.** A status token can also be allowed to see where each car is, coarsely: `place`, the name of a place you gave (never a street address; `null` at a spot the site named itself or on the road) and `at_home`, true or false (`null` when the car's position is unknown). Nothing finer is added: no coordinates, address, heading or speed. It is off by default and per token. Turn it on in Settings › Sign-in and API (the switch on the token, which takes effect at once with no reconnect), or tick the box on the Allow page when connecting; an app that skips the Allow page keeps the choice it was allowed with. An alert goes out when it is turned on. A token cannot turn it on for itself.

**Tokens do not expire.** Revoking one in Settings is the only thing that stops it. Any `/api/` call with a token that is not valid, with no token at all, or with the token in the URL (which is never read) answers `401 {"error": …}` as JSON, never the sign-in page: **401 means send a token, or reconnect**. A method the path does not take answers `405` with an `Allow` header, and a parameter that makes no sense (`month=xx`) answers `400` naming what it should be. A stream that is open when its token is revoked closes.

The app keeps the token in its secure storage and sends it as `Authorization: Bearer …`, which works with sign-in on like every other `/api/` call.

## Live stream

`GET /api/stream` is a server-sent event stream for apps, with the same token or session as the other `/api/` endpoints. It sends one `now` event per car, whose JSON data is what `/api/now` answers for that car, on one line:

```
event: now
data: {"vin":"…","name":"…","soc":58,"charge_minutes_left":42,…}

```

Every connection starts with every car's full current state, then an event only for a car whose state has changed since, so a quiet car is silent. Cars are looked at about every 15 seconds. A comment line (`: keep`, followed by a blank line) goes out whenever nothing has been sent for 20 seconds, so a connection that has been silent for longer than that is dead: reconnect. There is no `Last-Event-ID` and no replay; nothing is missed by reconnecting, because the first events of the new connection are the current state.

Each bearer token may hold four streams open at once (sessions and the sign-in-off site are not capped); the fifth is refused with `429 {"error": "too_many_streams"}`, so close a stream before opening another. `?vin=` limits the stream to one car; a token for one car must name it, as everywhere else. An unknown car is a 404.

`charge_minutes_left` (in `/api/now` too) is whole minutes until the charge reaches its target: the connector's own estimate when it reports one, otherwise the energy still to add over the present charging power. It is `null` when the car is not charging or the facts are missing. The estimate runs a little short on a fast charge, because the power tapers near the top.

`color` and `color_dark` (in `/api/now` too) are the car's colour on the site's charts and maps, as `#RRGGBB`, for a light ground and a dark one. The owner can change it on the car's page.

## Endpoints

Car endpoints take `?vin=` for one car or `?vin=all` for the garage; without it, `/api/now` answers about the live car (the first car the connector reports, else the first imported car by name), and the answer's `vin` and `name` say which; with several cars, `?vin=all` lists every one. `/api/whoami` takes no `?vin=`; `/api/health` answers for every car, or one with `?vin=` (a single-car token must name it).

| Endpoint | Returns |
|---|---|
| `GET /api/whoami` | the token's label, its car (`null` for the whole garage) and its kind (`read`, `control` or `admin`) |
| `GET /api/now` | the car's state now: battery, range, position, what it is doing; a list for `?vin=all` |
| `GET /api/drives` | the 50 most recent drives, newest first; `?since=YYYY-MM-DD&until=YYYY-MM-DD` (local days, `until` excluded, either optional) lists that range instead, up to 5,000 |
| `GET /api/charges` | the 50 most recent charges, newest first (the same `since` and `until` as drives), with cost and its source (`cost_source`); a home charge whose energy has been split adds `src_status` (`ok`), `src_solar_kwh`, `src_battery_kwh`, `src_grid_kwh`, `src_sun_frac` (the share of the Powerwall's part that was solar), `src_grid_cost`, `src_battery_cost` and `src_approx` |
| `GET /api/stream` | server-sent events: a `now` event per car when its state changes (see Live stream) |
| `GET /api/energy` | the house's Powerwall now and its kWh today (see Energy) |
| `GET /api/energy/hours?since=YYYY-MM-DD&until=YYYY-MM-DD` | the house's kWh per clock hour for those local days, `until` defaulting to tomorrow, at most 400 days (see Energy) |
| `GET /api/health` | the hourly data checks per car |
| `GET /api/driveprint` | every 100 m cell the car has reported from, as `[lat, lon, readings, car index]` |
| `GET /api/month-routes?month=YYYY-MM` | each drive's route that month, downsampled |

## Commands

`POST /api/cars/{vin}/command` sends one command to one car, with a **control token** (or a signed-in browser with its form token). The body is JSON, `{"command": "...", "value": ...}`; `value` only for the commands that take one. What a car takes, the tiers and the safety rules are in [commands.md](commands.md); commands must be on in Settings › Commands.

```sh
curl -X POST -H "Authorization: Bearer $CONTROL" -H "Content-Type: application/json" \
  -d '{"command": "charge_limit", "value": 80}' http://your-host:3044/api/cars/$VIN/command
```

```json
{"ok": true, "said": "Charge limit set to 80% 9:14 PM"}
```

| `command` | `value` | Tier |
|---|---|---|
| `charge_start`, `charge_stop` | | 1 |
| `charge_limit` | 50 to 100 (%) | 1 |
| `charge_amps` | 5 to 48 (A), Tesla | 1 |
| `climate_on`, `climate_off` | | 1 |
| `climate_temp` | 15 to 28 (°C, halves), Tesla | 1 |
| `defrost_on`, `defrost_off` | Tesla | 1 |
| `seat_heat` | 0 to 3, the driver's seat, Tesla | 1 |
| `lock`, `flash`, `honk`, `wake` | | 1 |
| `sentry_on`, `sentry_off` | Tesla | 2 |
| `sched_charge` | minutes after midnight, 0 to 1439, Tesla | 2 |
| `sched_charge_off` | Tesla | 2 |

Answers: 200 sent; 400 an unknown command or a value out of range; 403 refused (commands off, the car's tier, no way to reach the car, a command the car doesn't take, a token for another car, or a read or status token); 409 a tier 3 command, which needs the password entered again in a browser and is never sent with a token; 429 over the limit (10 a car a minute); 502 the car or Tesla said no. Every answer carries `said`, one sentence fit to show. Every call is in the car's command log, under the token's label.

## Energy

`GET /api/energy` answers about the house, once Settings › Energy reads one. Read tokens and a signed-in session may call it. A status token may not (403): the house's power over time shows when nobody is home, and its grid state shows outages. A token for one car may not either: the house is not any car's. `GET /api/energy/hours` has the same doors; its hours are UTC, so the fall-back night keeps both 1 a.m.s, each carries `coverage` (1.0 when every five-minute reading is there), and an hour with no readings is left out.

```json
{"reading": {"solar_w": 4200.0, "home_w": 1800.0, "grid_w": -300.0, "battery_w": -2100.0,
             "soe_pct": 61.4, "grid_status": "Active", "source": "cloud", "age_s": 4},
 "today": {"solar_kwh": 21.4, "home_kwh": 12.0, "grid_in_kwh": 1.5, "grid_out_kwh": 3.2,
           "battery_in_kwh": 5.0, "battery_out_kwh": 2.0}}
```

Powers are watts with a Powerwall's signs: `grid_w` is positive when importing and negative when exporting, `battery_w` positive when the Powerwall is giving to the house and negative when it is charging, and solar and home are never negative. `soe_pct` is on the Tesla app's scale. `source` is `cloud` (Tesla; the field is there for another source to come); `age_s` is how old the reading is, and readings are five minutes apart. `reading` is `null` when nothing was read in the last fifteen minutes; `today` is `null` when nothing has been kept since local midnight, and is kept a minute.

Times are ISO 8601 with their offset. Field names carry units where it matters (`kwh`, `max_mph`, `temp_f`). For whole histories, use the CSV exports.

## Admin tokens

**Network.** An admin token answers only from the local network or the tailnet: the connection's own address, and every address in `X-Forwarded-For` or `X-Real-IP`, must be private (`10/8`, `172.16/12`, `192.168/16`), on the tailnet (`100.64/10`), unique-local IPv6 (`fc00::/7`) or this machine. A request through Cloudflare (with `CF-Connecting-IP` or a `Cf-Access-*` header) is refused even when the tunnel's address is on the LAN. A request from the Tesla stream's network (`CC_TESLA_SUBNET`, `192.168.62.0/24` by default) is always refused. **Allow admin tokens from anywhere** in Settings lifts the rest of the rule; it is off by default. Docker's published port can hand the site its own gateway as the address (Docker Desktop on a Mac does), so a request forwarded from the internet to port 3044 can look local: don't forward that port. Read tokens work from anywhere.

**Audit log.** Every admin call, and every refused call from a known token, is logged: time, the token's label and last four characters, method and path, status, and client address. Settings shows the last 50; the log keeps 90 days. Refusals are capped at ten rows per token per minute, the rest counted into one row. The address is one the caller cannot choose: the connection's own, or, when that is a proxy on the local network or the tailnet, the address that proxy reported in `X-Forwarded-For`, followed by `via` and the proxy's address. A proxy that sets no `X-Forwarded-For` shows up as its own address.

Errors come back as `{"error": "…"}`: 400 for a request that makes no sense, 401 without an admin token, 403 for a read token or the wrong network, 404 for no such thing, 409 when an earlier step is missing. No answer carries a token, key, password or database address.

```sh
CC=http://your-host:3044 TOKEN=…   # an admin token, from the LAN or the tailnet
```

| Call | What it does |
| --- | --- |
| `GET /api/admin/tesla` | Connect Tesla's checklist: each step's state, the registered domain, the stream, and per car the key, the stream config, its last message and any errors. `?fresh=1` asks Tesla again. |
| `POST /api/admin/tesla/register` | Step 2: make the app's key if there is none, check the domain serves it, register it with Tesla. |
| `POST /api/admin/tesla/stream/register` | Register the stream address with Tesla. |
| `POST /api/admin/tesla/stream/start` `{"vin": …}` | Start one car's stream. |
| `POST /api/admin/tesla/stream/stop` `{"vin": …}` | Stop it, and ask Tesla to take the config out of the car. |
| `GET /api/admin/sources` | Every source kept in the app and how its poller is doing; never its secret. |
| `POST /api/admin/sources/{id}/test` | Test one, as its Test button does. |
| `GET /api/admin/places` | Every place with its address, icon and price. |
| `PATCH /api/admin/places/{id}` `{name?, icon?, price?, address?}` | Change any of those, checked as the Places forms check them. A price is `{"kind": "free"}`, `{"kind": "home"}`, `{"kind": "average"}`, `{"kind": "kwh", "rate": 0.30, "fee": 1}`, `{"kind": "minute", "rate": 0.25}` or `null`; an icon is an icon name, `"none"` or `null` for the default; an address of `null` goes back to the looked-up one. |
| `GET /api/admin/money` | The home plan, fast-charging and petrol prices, and the regional averages' state. |
| `POST /api/admin/money/averages/refresh` | Ask for a fresh copy of the regional averages (202: the background worker fetches it). |

```sh
curl -H "Authorization: Bearer $TOKEN" $CC/api/admin/tesla
curl -X POST -H "Authorization: Bearer $TOKEN" $CC/api/admin/tesla/register
curl -X POST -H "Authorization: Bearer $TOKEN" $CC/api/admin/tesla/stream/register
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"vin": "5YJ3E1EA0JF000001"}' $CC/api/admin/tesla/stream/start
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"vin": "5YJ3E1EA0JF000001"}' $CC/api/admin/tesla/stream/stop
curl -H "Authorization: Bearer $TOKEN" $CC/api/admin/sources
curl -X POST -H "Authorization: Bearer $TOKEN" $CC/api/admin/sources/3/test
curl -H "Authorization: Bearer $TOKEN" $CC/api/admin/places
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"name": "Work", "price": {"kind": "kwh", "rate": 0.30}}' $CC/api/admin/places/12
curl -H "Authorization: Bearer $TOKEN" $CC/api/admin/money
curl -X POST -H "Authorization: Bearer $TOKEN" $CC/api/admin/money/averages/refresh
```

## MCP

`POST /mcp` is a read-only [Model Context Protocol](https://modelcontextprotocol.io) server (Streamable HTTP, JSON replies, no session), so Claude or any MCP client can answer "how far did we drive last month", "what did charging cost", "battery health" or "where did Alpha park last night" from your own data. It only reads: no tool sends a command or changes anything, and no token kind but MCP opens it. It speaks both the current protocol revision (2026-07-28: the version in every request's `_meta` and an `MCP-Protocol-Version` header, `Mcp-Method` and `Mcp-Name` headers that must match the body, `server/discover`) and the earlier ones (`initialize`, 2025-11-25 down to 2024-11-05); a client picks whichever it speaks. Batches are refused (400).

Make the token in Settings › Sign-in and API › New token › MCP. The page then shows, once, the URL and the config to paste for Claude Code, Claude Desktop (through `mcp-remote`, which needs Node: Desktop's config file only starts local programs, and its connectors want OAuth) and `.mcp.json` or Cursor. Send the token as `Authorization: Bearer …` like every token. A browser session does not open `/mcp`, and a browser request whose `Origin` is not this site's is refused (403): the configured public address (`CC_PUBLIC_URL` in `stack/.env`, e.g. `https://your-host/carcommand`, which is also the address the snippets use), `localhost`, a private address, or a bare or `.local` name. The bearer is what actually keeps a web page out. Behind Cloudflare Access the client needs a service token (or use the LAN address).

```bash
export CC_TOKEN=…
claude mcp add --transport http car-command https://your-host/mcp --header "Authorization: Bearer $CC_TOKEN"
```

```json
{"mcpServers": {"car-command": {"type": "http", "url": "https://your-host/mcp", "headers": {"Authorization": "Bearer TOKEN"}}}}
```

Every tool call leaves a row (tool, token, time; never the arguments), and Settings › Sign-in and API lists the last 20 under the tokens, kept 90 days.

**What a token sees.** A token for one car sees that car only: the other cars are not listed, and asking for one by name is an error. By default an answer names a place only when you named it, otherwise the city; no VIN, latitude, longitude, street address, speed or heading, and `list_cars` gives names only (a VIN typed as `car` still finds the car). A token made with **Coordinates** on also gets all of those: street addresses, latitude and longitude, speed and heading, and the VIN. Notes you wrote on a drive pass through as written. That choice is made when the token is made; to change it, make another. Distances are miles, energy kWh, temperatures °F, times local to the box.

| Tool | Answers |
| --- | --- |
| `list_cars` | the name of each car the token sees (with the VIN, on a Coordinates token) |
| `current_status` | state, battery %, range, plugged in, charge progress, locked, odometer, outside temperature, software and the place (`car?`) |
| `list_drives` | drives in a period, newest first, one line each: when, minutes, miles, from, to, kWh, Wh/mile, battery before and after, cost (`car?`, `period?` or `since?`/`until?`, `limit?` up to 40, `offset?`) |
| `list_charges` | charges the same way: where, AC or DC, battery before and after, kWh, cost and whether it is an estimate |
| `get_drive`, `get_charge` | one by `id` (required), with top speed, weather, elevation, the rate plan and the sun/Powerwall/grid split |
| `stats` | miles, drives, energy used, Wh/mile, charged kWh and charging cost for a period (default this month); `by` = day, week or month adds a breakdown of up to 60 buckets |
| `battery_health` | range at 100 % now, first and best, and the loss in miles and per cent |
| `records` | the period's records, each with the drive or charge that set it |
| `top_routes`, `top_places` | the most-driven routes and most-visited places (`span` = all, year or 90d) |
| `software_versions` | versions run, when each arrived, days kept, range at 100 % on it, and how often updates come |

`period` is `today`, `yesterday`, `this_week`, `last_week`, `this_month`, `last_month`, `this_year`, `last_year`, `last_7_days`, `last_30_days`, `last_90_days`, `last_12_months`, `all`, a month (`2026-03`) or a year (`2025`). `since` and `until` take `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`; a bare `until` date includes that day. Lists page with `offset`: an answer holds `next_offset` while there is more. A car's position at a past moment is the `to` of the newest drive before it: `list_drives` with `until` set and `limit` 1.

Errors a model can fix (an unknown car, a bad date, an argument out of range) come back as a tool result with `isError`, in a sentence. A bad token is `401` with `WWW-Authenticate: Bearer`; another kind of token is `403`; `GET` and `DELETE` on `/mcp` are `405`; a body over 64 KB is `413`.

## CSV

Signed in, in a browser:

- `/drives.csv` and `/charges.csv`: every drive or charge for the car, or `?vin=all`. Also linked from the Drives and Charges pages.
- `/data.csv?date=YYYY-MM-DD`: every raw reading the car sent that day.

A backup of the whole database is `make backup`, or `carcommand backup` in the one container ([install](install.md#backups)). The database is plain Postgres.

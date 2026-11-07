# Car commands

Car Command can send a car what you'd otherwise open its app, Home Assistant or TeslaFi for: charging, climate, seats, security, the doors and windows, HomeLink and more (the table below). They're on the car's **Now** card: tiles for the lock, frunk, trunk, climate, charging and windows that say what the car is now and send on a tap, and under **More controls** (folded until opened; the browser remembers) a quick row (flash, honk, wake, keyless driving) and a panel of tabs (Charge, Climate, Seats, Security, More, Log). A tap on a tile or quick button sends at once; the panel's controls send a moment after the last tap. The ones that open, unlock or drive are confirmed first (closing up never is): with a passkey, one Face ID or Touch ID per command (cancelling the sheet sends nothing); otherwise a second tap, shown in the tile itself, and the PIN or password. Every command goes by `POST` only and is written to the car's command log (the **Log** tab).

Commands are **off** until you turn them on in **Settings › Commands**.

## What each car takes

| | Tesla | Lucid | Other CarConnectivity brands |
|---|---|---|---|
| Goes through | Tesla's signing proxy | the CarConnectivity connector (0.3.0 or later; keyless driving 0.4.0) | the CarConnectivity connector |
| Start and stop charging, charge limit | yes | yes | if the connector offers it |
| Charging current | yes (5 to 48 A) | yes (8 to 80 A) | no |
| Charge port open and close | yes | no (an Air's opens by hand) | no |
| Battery preconditioning | no (Tesla preconditions for a navigated Supercharger) | yes | no |
| Climate on and off, lock | yes | yes | if the connector offers it |
| Cabin temperature | yes | no | no |
| Defrost | yes | yes | no |
| Keep climate: Keep, Dog, Camp, off | all four | Dog (creature comfort) and off | no |
| Max A/C | no | yes | no |
| Cabin overheat protection: on, fan only, off (tier 2) | yes | no | no |
| Bioweapon defense | yes, where fitted | no | no |
| Seat heat, every seat, 0 to 3 | yes | yes, where fitted | no |
| Seat ventilation, front seats, 0 to 3 | yes, where fitted | yes, where fitted | no |
| Steering wheel heat | on and off | 0 to 3 | no |
| Lights, horn, wake | yes | yes | if the connector offers it |
| Headlights on and off | no | yes | no |
| Media: play or pause, next, previous, volume | yes | no | no |
| Sentry (tier 2) | yes | yes | no |
| Sentry at home, at work; enhanced deterrence; alarm on (tier 2); alarm silent or off (tier 3) | no | yes | no |
| Silence a Sentry alarm (tier 3) | no | yes | no |
| Scheduled charging, scheduled departure with preconditioning, with or without off-peak charging (tier 2) | yes | scheduled charging only (connector 0.5.0): a start and a finish time, on or off, with no place; no departure timer exists in Lucid's API | no |
| The car's schedule, read-only on Now | yes, from the stream | yes (connector 0.5.0; read from the car's own schedule once an hour and after a change) | no |
| Unlock (tier 3) | yes | yes | if the connector offers it |
| Open the trunk or frunk (tier 3) | yes | yes | if the connector offers it |
| Close the trunk or frunk (tier 2) | no (by hand, or the trunk button again) | yes | if the connector offers it |
| Vent the windows (tier 3), close them (tier 2), keyless driving (tier 3) | yes | yes | no |
| HomeLink (tier 3), sent with where the car is | yes | no (no HomeLink in Lucid's API) | no |

A car reached only through an import (a TeslaFi export, a TeslaMate database) takes no commands, and the demo car never does.

**Never:** nothing moves the car (no Summon), and nothing installs software (the car can't be driven for half an hour; the car or its app, with the release notes in view, decides), changes a PIN, valet mode or a speed limit (each takes the car's PIN), adds or removes keys, or plays a noise. A name outside the list above is refused.

## Tiers

| Tier | Commands | What it takes |
|---|---|---|
| 1, Everyday | charging, its limit and current, the charge port, preconditioning, climate, temperature and keeping it on, defrost, Max A/C, bioweapon defense, seats and wheel, lights, horn, wake, headlights, media, lock | commands on, a confirming tap |
| 2, Settings | Sentry and its siblings (at home, at work, enhanced deterrence, turning the alarm on), cabin overheat protection, scheduled charging and departure, closing the windows or a Lucid's trunk or frunk (closing up asks for no confirmation) | as tier 1, and the car set to **+ Settings** in Settings › Commands |
| 3, Access | unlock, opening the trunk or frunk (a Tesla's trunk button too, which opens or closes), venting the windows, HomeLink, keyless driving, and every alarm disarm (off, silent, silencing it) | as tier 2 with **+ Access**, and confirmed every time: a passkey for that one command, or the command PIN or the site's password entered in the last two minutes |

## Safety

- **Sign-in in front.** Commands need the site's password or Cloudflare Access. With sign-in off, every command is refused.
- **Owners.** Whoever signs in with the password is the owner. Through Cloudflare Access, an identity is an owner only when `CC_ACCESS_EMAILS` in `.env` names it, since an Access policy may let in a whole domain; a service token never is. Anyone admitted sends tier 1 and 2; only an owner sends tier 3 or changes the settings below.
- **Raising power asks first.** Turning commands on, raising a car's tier, minting any token, turning on admin tokens from anywhere, and setting the garage to open (or changing its car, home or door while it opens) take a confirmation, as tier 3 does: Face ID with a passkey, else the PIN or the password. Lowering or turning things off never asks. Until a passkey, a PIN or a password exists there is nothing to ask for, and these save as before.
- **POST only, with the page's form token.** No command is reachable by a link, and none is ever in a URL.
- **Confirming tier 3.** Settings › Access › Confirming commands holds the passkeys and the command PIN. A passkey (Face ID, Touch ID or a security key) needs the site on https at its name; it confirms one command per use and cannot be replayed. The PIN (six to eight digits) and the password work on the LAN address too and cover two minutes; over plain http from off the home network a tier 3 command is refused, so use the https address there. A Cloudflare Access sign-in is not a confirmation: the identity provider lets a signed-in phone straight through. With none of them set up, tier 3 is refused and says to add one. The first passkey or PIN needs nothing more than being signed in; after that, adding, removing or changing one needs a confirmation first. Every passkey or PIN event (added, removed, a wrong PIN, a refused passkey, the PIN or password used while a passkey exists) is an alert on the sign-in channels, and the command log says how each tier 3 command was confirmed. Lost the phone? `make passkeys-reset` on the host forgets every passkey and the PIN; the password is kept.
- **Rate limits.** At most 10 commands a car a minute. Each tier 3 command to a car an hour: unlock 3, keyless driving 2, the others 6; and 10 tier 3 commands an hour across all cars.
- **The log.** Every command asked for, sent or refused: when, which car, what, its argument, who (the site, the Access identity, an API token's label, or the charging hold), the address it came from, the result and how long it took. Kept a year.
- **Alerts.** Every tier 3 try, sent or refused, goes to every alert channel at high priority, and no switch in Settings › Alerts mutes it; a car cannot be set to **+ Access** until an alert channel exists. A tier 2 command goes out as **Car commands** in Settings › Alerts, and so does a charging hold that could not pause or start a car.
- **The switch.** Settings › Commands › **Send commands to the cars** off stops every command and the charging hold at once.

## Tesla: the signing proxy

A Tesla takes a command only when it is signed with the key the car already holds from Connect Tesla. Car Command uses Tesla's own signer, `tesla-http-proxy` from [teslamotors/vehicle-command](https://github.com/teslamotors/vehicle-command) (0.4.1, pinned by digest). It holds the app's private key in a file only its own user can read, listens only where the site can reach it, and speaks to the site over TLS with a certificate made for it at start, which the site pins.

- **The one container** runs it by itself, unless `TESLA_PROXY_URL` points at a proxy of your own. Until Connect Tesla has made the app's key it waits; then it starts within a minute.
- **Compose:** start it and point the site at it.

  ```sh
  echo 'TESLA_PROXY_URL=https://tesla-proxy:4443' >> stack/.env
  docker compose -f stack/docker-compose.yml --profile commands up -d tesla-proxy web
  ```

  The site writes the key and certificate into the `tesla_proxy` volume; the proxy reads them as the site's user id, on a network nothing else joins.

Commands also need the `vehicle_cmds` scope on your Tesla sign-in. An account that signed in before commands existed sees **Sign in again to allow commands** on the Now card and on Connect Tesla; the button runs the usual sign-in and Tesla asks only for what is new. Until then, charging commands still work (the `vehicle_charging_cmds` scope covers them) and the card says **Charging only**.

Scheduled charging and departure use Tesla's current schedule commands on firmware 2024.26 and later, and the legacy `set_scheduled_charging` and `set_scheduled_departure` before it (the firmware is the car's reported version; one that cannot be read counts as current). Every day, under ids of Car Command's own (4301 to 4303), so sending again changes the schedule instead of adding another. Scheduled charging is a charge schedule that starts at the time; a departure is a preconditioning schedule for that time and, with off-peak charging, a charge schedule that must finish by it; Off removes the schedules Car Command made, not ones set in the car or Tesla's app.

The place: the proxy refuses `add_charge_schedule` and `add_precondition_schedule` without a latitude and longitude, and the car keys each schedule to where it applies, so it cannot be left out. You pick it in **Applies at** on the Charge tab, in this order: the car's own Home and Work (only when the car says them, see below), Car Command's places tagged Home or Work, **Where the car is now**, then every other named place. Tesla's Home is the default, else where the car is. The choice travels as an id (`tesla:home`, `place:12`, `car`); the site looks the position up itself, so a request cannot name coordinates. The position goes to Tesla in that one command. It is not logged and not stored: the only positions held are the car's Home and Work from its last read (memory only, gone on restart), and Car Command's own places, which are already stored. Off, and the legacy commands (firmware before 2024.26), send no place.

What Tesla exposes of Home and Work: there is no endpoint for navigation favourites or saved locations. What exists is the car's `charge_state` carrying `home_location` and `work_location` in its protobuf definition (vehicle-command `vehicle.proto`), and the schedule commands' batch removal by home, work or other; Fleet Telemetry has `LocatedAtHome` and `LocatedAtWork` flags, not positions. Whether the Fleet REST `vehicle_data` returns `home_location` and `work_location` is not documented and not verified here: Car Command asks (`charge_state` rides the schedule read) and offers Home and Work only if they come back. A schedule's own `name` is a label, not Home or Work, so the tile names a schedule's place by matching its position to those two, then to your places within 150 m, else "elsewhere".

What Now shows comes from the car: after a schedule command, and in the daily identity read of an awake car, Car Command asks `vehicle_data` for `charge_schedule_data` and `preconditioning_schedule_data` (it never wakes a car) and keeps the times and the name of the place each applies to in memory, never the positions. Until it has read them, Now shows what the stream's `ScheduledChargingMode`, `ScheduledChargingStartTime` and `ScheduledDepartureTime` say.

A sleeping car is woken first (up to 45 seconds), then the command is sent. Tesla bills commands per call; the log shows how many went.

## Lucid and other brands: the connector

A car on the CarConnectivity connector takes the commands its connector offers. The site sends them to the connector's REST plugin, so the brand's own sign-in never leaves the connector. The Lucid connector offers commands only when its config says `"commands": true`; a connector that offers none shows **This car's connector takes no commands** on the Now card, and a command it doesn't offer is refused with that reason.

## The charging hold

For a time-of-use plan: keep the car from charging at home through the expensive hours, and start it again when they end.

Turn it on per car in **Settings › Commands › Charging hold**, with its hours (06:00 to 00:00 unless you change them: charging at home waits for midnight). Then, while commands are on:

- A charge at home that runs inside the hours is paused after 10 minutes. The grace is for a car that charges a few minutes after it is plugged in and then stops on its own schedule.
- A charge already running when the hours begin, the night's charge finishing in the morning, is left to finish.
- It starts again half a minute after the hours end, unless the car is already at its limit or no longer home.
- Start it again yourself after a pause, and the hold leaves it alone until the hours end.
- Unplug and the hold forgets the pause: a car that leaves and comes back is held again.
- A pause that doesn't take is sent again once, three minutes later; a start that doesn't take is said ten minutes later.

The hold runs once a minute in the background, from what the car last reported (the Tesla stream, a TeslaFi feed or the connector), and its pauses and starts are in the car's command log as **hold**.

## From an app: control tokens

Mint a **Control** token on Settings › Access. It reads what a read token reads and may also send commands; no other kind of token ever can. It sends everyday commands, or car settings too if you choose (never tier 3: that needs you), and never expires unless you set it to (in a week to a year). A token made before tiers sends everyday commands. See [api.md](api.md#commands).

## Tesla API use and budget

Settings › Commands › Tesla API counts every call Car Command makes to Tesla's Fleet API, by car and kind (data requests, commands, wakes, streaming setup, and the free sign-in calls), per calendar month (UTC). Tesla's terms as of 2026-10 (https://developer.tesla.com/docs/fleet-api/billing-and-limits): a US$10 monthly credit per account, then pay per use, about $0.002 a data request, $0.001 a command, $0.02 a wake and $0.0001 a streamed signal (rates summarised at https://teslemetry.com/blog/tesla-fleet-api-pay-per-use). Tesla changes these, so the allowance and the rates are settings. Streamed signals are billed as the cars stream through the receiver and are not in the count, so the figure is a floor.

The budget acts on the share of the allowance spent: from 50 % background polling (the facts refresher, charging bills, the car identity read, the Powerwall live read) waits twice as long; from 80 % four times as long, background wakes are refused and one alert goes to the "Tesla API allowance" channel; at 100 % background calls stop until the allowance is raised. Commands, whether sent by hand or by the charging hold, always go, with a note past the limit. Sign-in and token refresh are never held back.

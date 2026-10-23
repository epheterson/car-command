# Car commands

Car Command can send a car what you'd otherwise open its app, Home Assistant or TeslaFi for: charging, climate, seats, security, the doors and windows, HomeLink and more (the table below). They're on the car's **Now** card, under the battery. Every command asks for a second tap, goes by `POST` only, and is written to the car's command log (on the Now card, under **Command log**).

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
| Sentry at home, at work; enhanced deterrence; alarm on, silent, off (tier 2) | no | yes | no |
| Silence a Sentry alarm | no | yes | no |
| Scheduled charging (tier 2) | yes | no | no |
| Unlock (tier 3) | yes | yes | if the connector offers it |
| Open the trunk or frunk (tier 3) | yes | yes | if the connector offers it |
| Close the trunk or frunk (tier 2) | no (by hand, or the trunk button again) | yes | if the connector offers it |
| Vent or close the windows, keyless driving (tier 3) | yes | yes | no |
| HomeLink (tier 3), sent with where the car is | yes | no (no HomeLink in Lucid's API) | no |

A car reached only through an import (a TeslaFi export, a TeslaMate database) takes no commands, and the demo car never does.

**Never:** nothing moves the car (no Summon), and nothing installs software (the car can't be driven for half an hour; the car or its app, with the release notes in view, decides), changes a PIN, valet mode or a speed limit (each takes the car's PIN), adds or removes keys, or plays a noise. A name outside the list above is refused.

## Tiers

| Tier | Commands | What it takes |
|---|---|---|
| 1, Everyday | charging, its limit and current, the charge port, preconditioning, climate, temperature and keeping it on, defrost, Max A/C, bioweapon defense, seats and wheel, lights, horn, wake, headlights, media, silencing an alarm, lock | commands on, a confirming tap |
| 2, Settings | Sentry and its siblings (at home, at work, enhanced deterrence, the alarm mode), cabin overheat protection, scheduled charging, closing a Lucid's trunk or frunk | as tier 1, and the car set to **+ Settings** in Settings › Commands |
| 3, Access | unlock, opening the trunk or frunk, vent or close the windows, HomeLink, keyless driving | as tier 2 with **+ Access**, and the site's password entered again (or a Cloudflare Access sign-in) within the last five minutes, every time |

## Safety

- **Sign-in in front.** Commands need the site's password or Cloudflare Access. With sign-in off, every command is refused.
- **POST only, with the page's form token.** No command is reachable by a link, and none is ever in a URL.
- **Rate limits.** At most 10 commands a car a minute, and 3 tier 3 commands a car an hour.
- **The log.** Every command asked for, sent or refused: when, which car, what, its argument, who (the site, the Access identity, an API token's label, or the charging hold), the address it came from, the result and how long it took. Kept a year.
- **Alerts.** A tier 2 or 3 command goes out on your alert channels (the **Car commands** kind in Settings › Alerts), and so does a charging hold that could not pause or start a car.
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

Mint a **Control** token on Settings › Sign-in and API. It reads what a read token reads and may also send commands; no other kind of token ever can. See [api.md](api.md#commands).

## Tesla API use and budget

Settings › Commands › Tesla API counts every call Car Command makes to Tesla's Fleet API, by car and kind (data requests, commands, wakes, streaming setup, and the free sign-in calls), per calendar month (UTC). Tesla's terms as of 2026-10 (https://developer.tesla.com/docs/fleet-api/billing-and-limits): a US$10 monthly credit per account, then pay per use, about $0.002 a data request, $0.001 a command, $0.02 a wake and $0.0001 a streamed signal (rates summarised at https://teslemetry.com/blog/tesla-fleet-api-pay-per-use). Tesla changes these, so the allowance and the rates are settings. Streamed signals are billed as the cars stream through the receiver and are not in the count, so the figure is a floor.

The budget acts on the share of the allowance spent: from 50 % background polling (the facts refresher, charging bills, the car identity read, the Powerwall live read) waits twice as long; from 80 % four times as long, background wakes are refused and one alert goes to the "Tesla API allowance" channel; at 100 % background calls stop until the allowance is raised. Commands, whether sent by hand or by the charging hold, always go, with a note past the limit. Sign-in and token refresh are never held back.

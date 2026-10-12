# Making the Tesla app

Car Command reads a Tesla directly through Tesla's Fleet API, with a developer app of your own. **Settings › Cars and sources › Connect Tesla** (`/settings/sources/tesla`) is a five-step checklist; this page covers what each step needs.

## What you get

| | Without the stream | With the [stream](tesla-stream.md) |
|---|---|---|
| The account's cars, named and identified | yes | yes |
| What Tesla billed for each Supercharger session ([costs](costs.md)) | yes | yes |
| A Powerwall: its live row on Now, and each home charge split into sun, Powerwall and grid | yes | yes |
| Drives, charges, sleep, the car's live state | no | about once a second |

Car Command does not poll a car for its state, and never wakes one. Live data comes only from the stream, which needs Docker Compose for now. Without it, a TeslaFi or TeslaMate you already run can be the live source ([sources](sources.md#tesla)).

## What you need

- A developer.tesla.com account with a card on file. Tesla bills its API per use; its pricing page has the current rates.
- The site on HTTPS at a hostname you control, such as `carcommand.your-domain.example`. Tesla sends your browser back there after the sign-in, and reads the app's public key from it.
- The key's path reachable from the internet with no sign-in in front of it (see [The key](#the-key)).
- The three settings below, in the site's environment.

## The settings

| Name | Value |
|---|---|
| `TESLA_CLIENT_ID` | the app's Client ID, from developer.tesla.com |
| `TESLA_CLIENT_SECRET` | the app's Client Secret |
| `TESLA_DOMAIN` | the hostname alone, such as `carcommand.your-domain.example`: no `https://`, no path |
| `TESLA_REDIRECT_URI` | only when the site is under a path on that domain: `https://carcommand.your-domain.example/carcommand/settings/sources/tesla/callback` |

They are read only from the environment; the page cannot set them.

- Compose: put them in `stack/.env`, then `docker compose -f stack/docker-compose.yml up -d --no-deps web`. A plain restart keeps the old environment.
- The one container: `docker stop carcommand && docker rm carcommand`, then the run line again with `-e TESLA_CLIENT_ID=... -e TESLA_CLIENT_SECRET=... -e TESLA_DOMAIN=...`. The data stays in the volume.

## Step 1: the app

Make a Fleet API app at developer.tesla.com. Step 1 on Connect Tesla shows the values to enter, each with a Copy button:

- **Grant types:** Authorization Code and Machine-to-Machine. Machine-to-Machine is what registers the domain with Tesla.
- **Allowed origin:** `https://<TESLA_DOMAIN>`.
- **Redirect URI:** `https://<TESLA_DOMAIN>/settings/sources/tesla/callback`, or your `TESLA_REDIRECT_URI`.
- **Scopes:** `openid`, `offline_access`, `vehicle_device_data`, `vehicle_location` and `vehicle_charging_cmds`. Tesla files its charging history and invoices under `vehicle_charging_cmds`; that scope also allows charge commands, and Car Command sends none. For a Powerwall, also allow `energy_device_data`. An app without it still signs in for cars.

Then set the three values above. The step is done once Tesla gives the app a token.

## Step 2: the key

**Publish and register** makes the app's key pair, keeps the private half sealed with `CC_SECRET_KEY`, and serves the public half at:

```
https://<TESLA_DOMAIN>/.well-known/appspecific/com.tesla.3p.public-key.pem
```

The site fetches that address from outside, the way Tesla will, then registers the domain with Tesla. It registers in Tesla's North American region; accounts in other regions are untested.

### The key

The path must answer from the internet with the key itself, not a sign-in page. The site serves it without a password. A proxy or access layer in front must let that one path through:

- **Cloudflare Access:** add a second Access application for `<TESLA_DOMAIN>/.well-known/appspecific/com.tesla.3p.public-key.pem` with a Bypass policy for everyone. Leave the rest of the site behind your normal policy.
- **A site under a path:** proxy that path at the domain's root to the site ([reverse proxy](install.md#behind-a-reverse-proxy)).

With the [stream](tesla-stream.md) set up (`TESLA_STREAM_HOST`), the key moves to the stream's Funnel address, because Tesla keeps one domain per app and the cars only stream to that domain. `TESLA_DOMAIN` then only needs to reach the site from your own browser for the sign-in.

## Step 3: sign in

**Sign in with Tesla** goes to Tesla's sign-in for the account the cars are on, and comes back to the redirect URI. The link is good for ten minutes, once. The refresh token is sealed with `CC_SECRET_KEY`.

## Step 4: the key in each car

Each car needs the app's key before it will stream. The step lists the account's cars, each with a link and a QR code. Open the link on the phone that has the Tesla app, near the car, and confirm with the key card. A car holds at most 20 keys; the page shows how many are used. **Check again** reads the cars once more.

## Step 5: the stream

Set up in [Tesla live stream](tesla-stream.md).

## When a step says no

| Message | What it means |
|---|---|
| Add the app's keys and domain to .env first (step 1). | `TESLA_CLIENT_ID`, `TESLA_CLIENT_SECRET` or `TESLA_DOMAIN` is missing from the site's environment, or the container was restarted, not recreated. |
| Tesla refused (…): … on step 1 | Usually a wrong Client ID or Secret, or an app without the Machine-to-Machine grant. |
| `<domain>` did not answer (…). | The key's address is not reachable from the internet: DNS, the certificate, or the proxy. |
| `<domain>` answered 302, 401 or 403 for the key. That is a sign-in page in front of it… | An access layer guards the key's path. Add the bypass above. |
| `<domain>` answered 404 for the key. | The path does not reach the site, often a site under a path with the root not proxied. |
| `<domain>` serves a different key at that path. | Something else answers there, or a cache holds an old key. |
| Nothing secret can be kept yet: CC_SECRET_KEY … | `CC_SECRET_KEY` is missing or not a valid key. |
| That sign-in link is spent or too old. Start again from step 3. | The sign-in took over ten minutes, or the return page was opened twice. Sign in again. |
| The Tesla sign-in was cancelled. | Tesla's page was closed or refused. |
| Tesla refused (…) on the sign-in | Often a redirect URI that does not match the app's exactly. |
| Tesla needs a new sign-in. | The refresh token stopped working. Sign in again on step 3. |
| Tesla lists no cars on this account. | The sign-in was for an account with no cars. |
| … no longer holds the app's key, so Tesla dropped the stream. | The key was removed from the car. Add it again (step 4), then **Start** again on step 5. |

## Cost

Tesla bills Fleet API requests and streamed signals to your developer account, beyond a free allowance. Without the stream, Car Command asks Tesla little: the car list and each car's key status while Connect Tesla is open, each car's identity at most once a day, and the Supercharger bills. The stream asks for a reading only when it changes by enough to matter ([details](tesla-stream.md)). The Powerwall's history import shows its estimated cost before you start.

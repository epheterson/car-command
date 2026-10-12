# Install and run

## What you need

Docker on an amd64 or arm64 host: a small server, a NAS that runs containers, a Mac mini, a Raspberry Pi 5 with an SSD.

- **Docker.** Compose installs also need Docker Compose v2 (`docker compose`, not `docker-compose`), `git`, `make`, `bash` and `openssl`.
- **Memory:** about 650 MB for the site, the database and the connector together (site 330 MB, database 170 MB, connector 125 MB). The [local maps add-on](maps.md) uses about 2.5 GB more.
- **Disk:** three cars with eight years of history take 1.2 GB of database. The maps add-on needs several GB more ([sizing](maps.md#sizing)).
- **Port 3044** free on the host. It is the only port published. Move it with `-p 8080:3044` (one container) or `CC_PORT` in `stack/.env` (compose).
- **Per source:** Lucid needs only the account. Tesla direct needs a developer.tesla.com account with a card on file and a hostname you control serving the site over HTTPS ([Tesla app](tesla-app.md)); its live stream also needs a Tailscale account with Funnel. TeslaFi needs an API token per car; TeslaMate, its database reachable from this stack.

The one container is the simple way. Docker Compose runs the same parts as separate containers, and is needed for the local maps add-on and the [Tesla live stream](tesla-stream.md) for now.

## The one container

```sh
docker run -d --name carcommand --restart unless-stopped --stop-timeout 30 -p 3044:3044 -v carcommand:/data ghcr.io/epheterson/car-command
```

Get the setup code with `docker exec carcommand carcommand web python3 setup_code.py`, open `http://<host>:3044` and choose a password with it. Add a source on **Settings › Cars and sources**, or [import](sources.md#importing-history) a TeslaFi export or a TeslaMate database. Units, currency, time zone and rates are in Settings.

The first start generates the database password, the connector's passwords and the key that seals your sources (`CC_SECRET_KEY`), creates the database, and starts the site and the connector.

To look around before adding your own cars, run the [demo](#try-it-with-demo-data) in a container of its own. `docker stop carcommand && docker rm carcommand && docker volume rm carcommand` removes the install and all its data.

`--stop-timeout 30` gives Postgres time for a clean shutdown after the site and connector stop; Docker's default is ten seconds. A database killed mid-write is not lost, but its next start replays its log. Stop the container before removing it; don't use `docker rm -f`.

### What is in the volume

Everything is under `/data`, in the `carcommand` volume:

- `postgres/`: the database (Postgres 18).
- `secrets/`: one file per generated value (`DB_PASSWORD`, `WEBUI_PASSWORD`, `REST_PASSWORD`, `CC_SECRET_KEY`), readable only by root inside the container. CarConnectivity and its plugins run as their own user without any of it, and use a database role limited to their own tables.
- `logs/`: each part's log, about 6 MB at most per part. The same lines go to `docker logs carcommand`; add `--log-opt max-size=10m --log-opt max-file=3` to the run line to cap that.
- `connector/`: files the connector reads, such as a Lucid `lucid-token.json` from an older install.
- `plugins/`: connectors for other brands, when added.

A second container on the same volume is refused.

### Settings

The settings a compose install keeps in `stack/.env` are environment variables here, with the same names, all optional: `-e NAME=value`, or `--env-file`. [`stack/.env.example`](../stack/.env.example) describes them.

Not used here: `DB_USER`, `DB_NAME`, `CC_PORT`, `CC_SUBNET`, `CC_TESLA_SUBNET` and `COMPOSE_PROJECT_NAME`. Publish a different port with `-p 8080:3044`.

A changed `-e` takes a new container, since `docker restart` keeps the old environment: `docker stop carcommand && docker rm carcommand`, then the run line again with the new value. The data stays in the volume.

The passwords and `CC_SECRET_KEY` are read from the environment only on the first start. After that the file in `/data/secrets` wins and a different value in the environment is ignored with a warning, so a changed flag can't lock you out. To change one, change its file and restart. A different `CC_SECRET_KEY` leaves every source kept in the app unreadable, and each has to be entered again.

`CC_CONNECTORS` names connectors built into the image, so it is a build argument: `make one-container CC_CONNECTORS='...'`. Brands added on **Settings › Cars and sources** need no rebuild.

### Commands inside it

`docker exec carcommand carcommand ...`:

- `status`: each part, running or waiting, and its restart count.
- `health`: Docker's healthcheck. The database is up and the site answers.
- `demo`: the demo car.
- `backup > FILE`: a database dump.
- `web COMMAND`: a command in the site's directory, as the site. `make` targets that run `docker compose exec web python3 ...` become `docker exec carcommand carcommand web python3 ...`, for example `teslafi_load.py` on an export copied in with `docker cp`.

A part that stops is restarted after 1, 2, 4 seconds and so on, at most five minutes apart. A site that stops answering is restarted. After an unclean stop, the next start waits for Postgres to replay its log and says so.

### Backups

```sh
docker exec carcommand carcommand backup > carcommand.dump
docker exec carcommand cat /data/secrets/CC_SECRET_KEY > carcommand.key   # keep it beside the dump, safely
```

The dump is a `pg_dump` in custom format, the same as `make backup`. The key is not in it: without the key, sources kept in the app have to be entered again.

To restore, stop the container, restore into its volume with the image, and start it:

```sh
docker stop carcommand
docker run --rm -i -v carcommand:/data ghcr.io/epheterson/car-command restore < carcommand.dump
docker start carcommand
```

The restore runs in one transaction, so a failed one leaves the database as it was. It refuses a volume that already holds drives, places, settings or a password; `restore --force` replaces them. Pass the dump's key with `-e CC_SECRET_KEY=...`: a new volume takes it, and a volume with a different key refuses unless forced.

### Upgrades

```sh
docker pull ghcr.io/epheterson/car-command
docker stop carcommand && docker rm carcommand
docker run -d --name carcommand --restart unless-stopped --stop-timeout 30 -p 3044:3044 -v carcommand:/data ghcr.io/epheterson/car-command
```

The data stays in the volume, and the schema upgrades at startup.

An image with a newer major Postgres refuses an older database. Move across with a backup from the old image and a restore into a new volume with the new one, since the old volume holds the old major's database:

```sh
docker exec carcommand carcommand backup > carcommand.dump
docker exec carcommand cat /data/secrets/CC_SECRET_KEY > carcommand.key
docker stop carcommand && docker rm carcommand
docker run --rm -i -e CC_SECRET_KEY="$(cat carcommand.key)" -v carcommand-new:/data ghcr.io/epheterson/car-command restore < carcommand.dump
docker run -d --name carcommand --restart unless-stopped --stop-timeout 30 -p 3044:3044 -v carcommand-new:/data ghcr.io/epheterson/car-command
```

Keep the old volume until the new install looks right, then `docker volume rm carcommand`.

### Moving a compose install into the one container

From the checkout that runs the compose install, with its database up:

```sh
make one-container                 # or: docker pull ghcr.io/epheterson/car-command, and IMAGE=ghcr.io/epheterson/car-command below
make one-container-move            # IMAGE=carcommand:local VOLUME=carcommand by default
```

`make one-container-move` needs a volume name that doesn't exist yet. It stops the compose site and connector, backs up into `backups/`, restores into the new volume with `CC_SECRET_KEY` from `stack/.env` (so sealed sources still open), copies `stack/secrets/lucid-token.json` if there is one, and writes the settings the one container reads into `stack/secrets/one-container.env`. Then it takes compose down without `-v`, so compose doesn't come back after a reboot; compose's database volume is left alone. If a step fails it says how to go back. It ends by printing:

```sh
docker run -d --name carcommand --restart unless-stopped --stop-timeout 30 -p 3044:3044 \
    --env-file stack/secrets/one-container.env -v carcommand:/data carcommand:local
```

Sign in with the same password. To go back: `docker stop carcommand` and `docker compose -f stack/docker-compose.yml up -d`. Compose's volume stays until you remove it (`docker compose -f stack/docker-compose.yml down -v`).

`MAPS_URL` and `TESLA_STREAM_HOST` are not carried over; the maps add-on and the Tesla stream stay with compose for now. A `TESLAMATE_DB_URL` naming a compose host must be reachable from the new container.

## Docker Compose

Commands here run from the checkout's root. The `make` targets marked "over ssh" run on the host named in `Makefile.local` ([another host](#running-it-on-another-host)); on the machine that runs the stack, use the `docker compose` command beside each.

```sh
git clone https://github.com/epheterson/car-command car-command && cd car-command
git checkout "$(git describe --tags --abbrev=0)"         # the newest release, not unreleased main
make setup                                                # writes stack/.env with generated passwords
docker compose -f stack/docker-compose.yml up -d db web   # Postgres, the schema, and the site
```

The site is on port 3044. Its first page asks you to choose a password and for the setup code: run `docker compose -f stack/docker-compose.yml exec web python3 setup_code.py` (`make setup-code` does the same over ssh). `make setup` never overwrites an existing `stack/.env`, which holds your database password.

Bringing up `web` also runs a one-shot `schema` container that creates the tables, so an install can run with no connector. The connector (`cc`) starts with `docker compose -f stack/docker-compose.yml up -d` once a live source needs it ([sources](sources.md)).

## Try it with demo data

`make demo` (in the one container, `docker exec carcommand carcommand demo`) generates six weeks of a fictional Model 3 around Springfield, Illinois as a TeslaFi export, imports it, and names its five places. The README's screenshots are of it.

The demo car and, on an install with no plan of its own, the demo's rate plan stay in whatever install you load them into, and there is no button to remove them. Look around in a throwaway container on another port:

```sh
docker run -d --name carcommand-demo -p 3045:3044 -v carcommand-demo:/data ghcr.io/epheterson/car-command
docker exec carcommand-demo carcommand demo        # then open http://<host>:3045
docker stop carcommand-demo && docker rm carcommand-demo && docker volume rm carcommand-demo
```

To start a compose install over on the data only: `docker compose -f stack/docker-compose.yml down`, then `docker volume rm carcommand_pgdata`. `down -v` also removes the built maps, the brand plugins and the Tesla stream's node identity (a new node needs a new auth key).

## Signing in

One password per household. The site asks you to choose one on first visit and keeps you signed in for a month. If sign-in is off (`CC_AUTH=off`), turn it on in **Settings › Sign-in and API**: choose the password and you are signed in at once, with no restart. The same page turns it off again for anyone who knows the password. Turning it on asks for the same setup code as `/setup`. Turning it on signs every other device out (unless `CC_SECRET` pins the signing key). It is stored as an scrypt hash; until it is set the site serves only the page that asks for it. Failed attempts are throttled.

The first password on `/setup` needs no code from this machine (loopback) or from a device on your home network that reaches the site directly (an address in 10/8, 192.168/16 or fc00::/7, with no proxy header, that is not on a network the container itself sits on), **in the first hour after the site first started** (a restart does not reset it; after that the code is needed, loopback included, because a header-less proxy on this machine connects from loopback). The window opens only on an install with no history (nothing in its auth settings and no car yet) at that start; a site that has run before, whether its signing key is in the database or pinned in `.env`, has the code from the start. The Settings switch that turns sign-in on, and the Cloudflare Access pin, are stricter: they always ask for the setup code, loopback included (you have a terminal), except that listing your e-mail in `CC_ACCESS_EMAILS`, or being signed in with the password, authorises the pin on its own. Everything else needs the setup code: the Docker bridge (172.16/12), the tailnet (100.64/10 and Tailscale's fd7a:115c:a1e0::/48), link-local, anything public, and anything a proxy passed on. A request counts as passed on if it carries any proxy header (`X-Forwarded-*`, `X-Real-IP`, `Forwarded`, `Via`, `CF-*`, `Tailscale-Funnel-Request` and similar), so **a proxy in front of the site must add `X-Forwarded-For`**: nginx's default `proxy_pass`, a TCP or stream proxy, `ssh -R` and frp add nothing, and during the first hour they would make the internet look like a home device. The container reads its own networks once. **In a bridge-network container (the compose default; found by a marker file, Kubernetes' environment, or containerd, kubepods, docker, libpod or lxc in the cgroup or mounts) every subnet it is attached to is distrusted, its gateway included**: on Docker Desktop (Mac, Windows) every published-port connection arrives from the compose network's gateway, often a 192.168.x address, so the code is always needed there. On a bare host or with host networking only the bridges and virtual networks (`docker*`, `br-*`, `virbr*`, `lxdbr*`, `cni*`, `podman*`, `flannel*`, `cali*`, `vxlan*`) are distrusted, and a phone on your LAN needs none; so on Linux with plain port mapping a phone keeps its own address and needs none. In Kubernetes (`KUBERNETES_SERVICE_HOST` set) no network counts as home and `/setup` always needs the code, because a NodePort's address translation makes the internet look like the node's own address. Containers on other Docker networks drawn from 192.168/16 or 10/8 pools reach the published port with their own addresses and count as home network during the first hour.

In `stack/.env` or the one container's environment:

- `CC_PASSWORD` sets the password from the environment, for a scripted install.
- `CC_AUTH=off` turns sign-in off, when a reverse proxy already decides who gets in. Cross-site request tokens are still checked. The proxy then has to be the only way in: anything that reaches the port directly, the whole LAN by default, reads every page and `/api/` without a password or token, live locations included. Publish the port on loopback (`CC_PORT=127.0.0.1:3044`; one container: `-p 127.0.0.1:3044:3044`) when the proxy runs on the same host, or keep sign-in on. The switch in Settings overrides this: once sign-in is turned on or off there, that choice is kept and `CC_AUTH` is no longer consulted.
- `CF_ACCESS_TEAM` names your Cloudflare Access team and `CF_ACCESS_AUD` its application's audience tag; see Behind Cloudflare Access. `CC_ACCESS_PROXY` and `CC_ACCESS_EMAILS` narrow who may present the Access token and who counts.
- `CC_SECRET` pins the session cookie's signing key. Unset, one is generated and kept in the database.
- `CC_SECRET_KEY` seals the sources kept in the app (TeslaFi tokens, a TeslaMate address, the Lucid and Tesla tokens). The one container generates it; under compose, `make setup` writes one and `make secret-key` (over ssh; locally `scripts/secret-key.sh`) adds one to an older install. It is in no database backup: lose it and those sources have to be entered again.

Everything else (alert channels, other brands, Connect Tesla, the network) is in [`stack/.env.example`](../stack/.env.example).


## Behind a reverse proxy

- The proxy must add `X-Forwarded-For`. Without a proxy header, during the first hour a request from the internet could look like a home device ([Signing in](#signing-in)).
- The site answers at `/`, `/carcommand` or `/cc`, with the prefix left on the path. Any other prefix breaks its links.
- Live updates (`/stream` and `/api/stream`) are server-sent events; the proxy must not buffer them.
- Keep the published port off the internet: `CC_PORT=127.0.0.1:3044` (one container: `-p 127.0.0.1:3044:3044`) when the proxy runs on the same host.

nginx:

```nginx
server {
    listen 443 ssl;
    server_name carcommand.your-domain.example;
    # ssl_certificate and ssl_certificate_key as usual

    location / {
        proxy_pass http://127.0.0.1:3044;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Caddy (it adds `X-Forwarded-For` and gets the certificate itself):

```caddy
carcommand.your-domain.example {
    reverse_proxy 127.0.0.1:3044 {
        flush_interval -1
    }
}
```

Under a path on a shared domain, proxy `/carcommand/` to `http://127.0.0.1:3044` without stripping the prefix (nginx: `location /carcommand/` with the same `proxy_pass` line, no path after the port). For [Connect Tesla](tesla-app.md), also proxy `/.well-known/appspecific/com.tesla.3p.public-key.pem` at the domain's root to the site, and set `TESLA_REDIRECT_URI` to the callback under the prefix.

## Behind Cloudflare Access

If the site is published through a Cloudflare tunnel with an Access application in front of it, Cloudflare can be the sign-in, with no Car Command password at all. Setup is one line and one tap.

1. Put your team and your own e-mail in `stack/.env`: `CF_ACCESS_TEAM=your-team` (the name before `.cloudflareaccess.com`; the full domain or URL works too) and `CC_ACCESS_EMAILS=you@example.com`, and recreate the site: `docker compose -f stack/docker-compose.yml up -d --no-deps web` (one container: `-e`, then a new container).
2. Open the site through its Cloudflare address, go to **Settings › Sign-in and API** and press **Use Cloudflare Access**. The page reads the signed token Cloudflare put on that very request, checks its signature against your team's published keys, and keeps the application's audience tag. Your team has other Access applications whose tokens are just as valid, so the tap works only for a person listed in `CC_ACCESS_EMAILS`, or with the setup code (type it beside the button), or from a password session; a service token never pins. The page learns nothing else from the request, and no host or link from it is ever kept. Sign-in is on from that moment, with no restart. **Stop using Cloudflare Access** on the same page undoes it. For a scripted install put `CF_ACCESS_AUD` (the tag, on the application's Overview page in Cloudflare) in `.env` too, and sign-in is on at start.

The team is never learned from a request. Anyone can run a Cloudflare team and sign a valid token with it, so the team must already be configured, in `.env` or typed into Settings by someone signed in with the password. The tap pins only the application, only from a token of that team, and never replaces a pin.

How a request is judged:

- It is signed in when it carries a valid Access token in the `Cf-Access-Jwt-Assertion` header: RS256, from your team, for your application, not expired. Nothing else on the request is believed, so an e-mail header written by hand opens nothing.
- The header is read only when the connection comes from a trusted address: by default any private, loopback or tailnet one (cloudflared beside the site). Narrow it with `CC_ACCESS_PROXY`, a comma-separated list of addresses or networks such as `172.18.0.0/16`. From anywhere else the header is ignored.
- A header from a trusted address that does not verify is refused with a 401. It never falls through to a password or a token.
- `CC_ACCESS_EMAILS`, a comma-separated list of e-mails, narrows who counts as signed in, and always applies to whoever pins. Unset, Cloudflare's own Access policy decides who is signed in, and pinning needs the setup code or the password.
- Cloudflare's keys are cached in memory and in the database, refreshed hourly, and keep working for up to a week if Cloudflare cannot be reached. After that, the header is refused.
- Stopping Cloudflare Access needs the password when there is one; otherwise a signed-in person (a service token cannot).
- A form token is tied to the signed-in identity, so a token one person loaded is no good for another.

**On your home network.** A request straight to the LAN address carries no token, so it falls back to the password (if you set one) or the login page. To use the site from home either set a password as the backup (Settings, or `/setup` before sign-in is on) or browse through the public address. With no password and no token a request gets a page that says where to sign in, and `/setup` is closed. API tokens work as before. While `CC_ACCESS_PROXY` is unset any private address may present the token (Settings says so); set it to your tunnel's address. Cloudflare Access only protects what goes through the tunnel, so keep the published port off the internet.

## Locked out

If you cannot get in, reset the sign-in. It forgets the sign-in switch chosen in Settings and the Cloudflare Access pin, keeps the password, and restarts the site, which then starts as `.env` says.

```sh
docker compose -f stack/docker-compose.yml exec web python3 signin_reset.py && docker compose -f stack/docker-compose.yml restart web
docker exec carcommand carcommand web python3 signin_reset.py && docker restart carcommand   # the one container
make signin-reset                                                                             # over ssh
```

Or set `CC_PASSWORD` and recreate the site; it replaces the stored password. Under compose: set it in `stack/.env`, then `docker compose -f stack/docker-compose.yml up -d --no-deps web`. In the one container: a new container with `-e CC_PASSWORD=...` (see [Settings](#settings)). A plain restart keeps the old environment.

## Backups under compose

```sh
make backup                                    # a dump in ./backups, safe while everything runs
make restore FILE=backups/carcommand-....dump
```

`make backup` writes a `pg_dump` in custom format under a temporary name, renames it when complete, and reads it back with `pg_restore -l`. It restores into a different Postgres version on another machine.

`make restore` reads the archive before touching the database, so a truncated file fails with your data intact. It refuses if the database holds anything (drives, charges, places, service records, settings or a password) and says what it found; `ARGS=--force` overrides. If it can't tell, it refuses.

## Upgrades under compose

```sh
make backup
git fetch --tags
git checkout "$(git describe --tags --abbrev=0 origin/main)"   # the newest release
docker compose -f stack/docker-compose.yml up -d --build
```

The one container gets releases only, and a compose install should too: `git pull` on `main` runs whatever was merged last.

Tables are created and columns added at startup; nothing is dropped or renamed, so an older dump restores into a newer build. A newer dump into an older build is not supported. CI upgrades an install with data in it on every change, for compose and the one container.

## Running it on another host

Put your host in `Makefile.local` (`MINI = user@host`, `DEST = path/on/host`). `make deploy` rsyncs `stack/` there (not `.env` or `secrets/`, and without deleting host-only files) and `make web` rebuilds the site container alone. `make import`, `make discover`, `make replay` and the rest run over ssh on that host.

`make staging` puts a second stack on the same host with a copy of production's data and reads every page against it, which catches a schema change or backfill that would lock the live site. `make staging ARGS=check` re-reads the pages, `ARGS=down` removes it. Set `COMPOSE_PROJECT_NAME` and `CC_PORT` to run a second stack beside the first; every name is keyed on the project.

## What is in the repository

- `stack/`: `cc` is the connector container (CarConnectivity with its database, web UI and REST plugins), `db` is Postgres, `web` is the site (FastAPI and Jinja), `aio` builds the one container, `maps` is the local maps add-on, `tesla` holds the Tesla stream's receiver and Funnel node.
- `scripts/`: setup, backup, restore, the move into the one container, staging, and the rate-data builders.
- `brand/`: the mark, lockups and social card.
- `docs/`: these pages and the screenshots.

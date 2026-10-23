# Troubleshooting

Commands below use `docker compose -f stack/docker-compose.yml` from the checkout's root for a compose install, and `docker exec carcommand carcommand` for the one container.

## Reading the logs

- One container: `docker exec carcommand carcommand status` shows each part, running or waiting, and its restart count. `docker logs carcommand` has every part's lines; each part's own log is in `/data/logs/` inside the container.
- Compose: `docker compose -f stack/docker-compose.yml logs --tail 200 web cc`.

## "no setup code"

`setup_code.py` prints `no setup code: a password is already set (or the site has not started yet)`. On a new install, wait a minute for the site's first start and run it again. If a password is set, sign in with it, or see [Locked out](install.md#locked-out).

## The setup code is asked for on my home network

The first password needs no code only from a device on your home network that reaches the site directly, in the first hour after its first start. On Docker Desktop (Mac, Windows), through a proxy, or after the first hour, use the code. [Signing in](install.md#signing-in) has the rules.

## "still CHANGE_ME"

The site or the connector stops at start with `... is still CHANGE_ME, the placeholder from stack/.env.example`: `stack/.env` was copied from the example rather than written by `make setup`. Move it aside, run `make setup`, and copy over any settings you had added. If the database already started with that password, give it the new one:

```sh
docker compose -f stack/docker-compose.yml exec db psql -U carcommand -c "ALTER ROLE carcommand PASSWORD 'the DB_PASSWORD from the new stack/.env'"
```

## Port 3044 is already in use

Docker says the port is already allocated, or the address is in use. Publish another one: `-p 8080:3044` on the one container, or `CC_PORT=8080` in `stack/.env` under compose.

## "Pool overlaps with other one on this address space"

Compose pins two subnets, `192.168.61.0/24` and `192.168.62.0/24`. If either is taken on your host, set `CC_SUBNET` or `CC_TESLA_SUBNET` in `stack/.env` to a free range.

## A change to `.env` or `-e` did nothing

`docker compose restart` and `docker restart` keep the old environment. Under compose: `docker compose -f stack/docker-compose.yml up -d --no-deps web` (or `cc` for the connector's settings). The one container: remove it and run it again with the new `-e`; the data stays in the volume. The passwords and `CC_SECRET_KEY` are read from the environment only on the first start ([Settings](install.md#settings)).

## `stack/secrets/lucid-token.json` is a directory

Docker made it when the connector started before the file existed, and `make lucid-login` then fails with `IsADirectoryError`. `make setup` now creates the file; on an older checkout replace the directory with an empty file:

```sh
sudo rm -r stack/secrets/lucid-token.json
touch stack/secrets/lucid-token.json && chmod 600 stack/secrets/lucid-token.json
```

An empty file means no token. Signing in to Lucid on **Settings › Cars and sources** needs no file at all.

## Lucid stops answering

Lucid rate-limits per account. Run one logger per account: not Car Command beside another tool polling the same Lucid account. The Lucid app alongside is fine.

## A brand added in Settings stopped

If a new brand stops the connector on its first start (usually a wrong password), it is taken out, its sign-in dropped, and the reason shown on **Settings › Cars and sources**. The other cars keep running. Under compose the connector must be running and able to reach PyPI the first time ([sources](sources.md#other-brands)).

## Cloudflare Access answers 401

A request with an Access token that does not verify is refused, never passed to the password. Check that `CF_ACCESS_TEAM` is your team's name, that the pinned application is the one in front of the site, and that the site can reach Cloudflare to refresh its keys. [Behind Cloudflare Access](install.md#behind-cloudflare-access) has the rules.

## Connect Tesla stops at a step

Each step shows Tesla's or the site's reason. [Making the Tesla app](tesla-app.md#when-a-step-says-no) lists them.

## "another container is using /data"

The one container refuses a second container on the same volume. `docker ps` shows the one that holds it; stop it first.

## Locked out

See [Locked out](install.md#locked-out).

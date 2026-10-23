# Tesla live stream (optional)

Each Tesla sends what changes (position, speed, gear, pack power, charging) about once a second while it drives or charges, to a receiver on your box. Car Command works drives, charges and sleep out of it every hour. The stream doesn't wake the car and sends nothing to it; commands are separate ([commands.md](commands.md)).

The stream also carries whether the car is locked, its doors and its windows (Tesla's `Locked`, `DoorState` and `FdWindow`, `FpWindow`, `RdWindow`, `RpWindow`), sent when they change, so `/api/now` has `locked`, `doors_open` and `windows_open` for a car on the stream: true or false, and null until the car has said. A car started before these fields were asked for shows a notice on Connect Tesla and is sent the new list automatically when the site starts; the car takes it when it next wakes.

Tesla bills each streamed signal, so the config asks for a number only when it changes by enough to matter (`minimum_delta`, documented by Tesla for numeric fields): position by 3 m, speed by 0.5 mph, heading by 5 degrees, pack voltage by 1 V and current by 2 A, and the charger readings by a small step. The route stays at a reading a second while the car moves, and the figures the site shows move by under 1 to 2 %. A parked or charging car, whose pack readings used to be sent every second, sends almost nothing. Tesla's own pricing page is the place to check the current rate.

## What it needs

Connect Tesla done through step 4 (your developer app, its key, sign-in, the key in each car; see [Making the Tesla app](tesla-app.md)), and a [Tailscale](https://tailscale.com) account with [Funnel](https://tailscale.com/kb/1223/funnel). Tesla's receiver ([fleet-telemetry](https://github.com/teslamotors/fleet-telemetry)) runs beside the site, a Tailscale node joins your tailnet as `carcommand-stream`, and Funnel carries the cars' connections to the receiver.

Either install runs it:

- **One container:** both are in the image and start only when you give it a Tailscale auth key. Each runs as its own user, away from the database and the key; the node keeps its identity in the volume (`/data/tesla`).
- **Compose:** two containers of the `tesla` profile, on a network of their own, apart from the rest of the stack.

## What the internet can reach

- `https://carcommand-stream.<your tailnet>.ts.net/.well-known/appspecific/com.tesla.3p.public-key.pem`: the app's public key, proxied to the site. No other path is served there.
- TCP port 8443 on that name, passed to the receiver, which only talks to clients holding a certificate Tesla issued to a car.

No port opens on your router. The node is tagged, and the policy below keeps it from reaching anything else on your tailnet.

## Steps

1. In the Tailscale admin console, turn on HTTPS certificates (DNS page), and add to the access policy a tag for the node, Funnel for that tag only, and no rule that lets the tag reach anything:
   ```json
   "tagOwners": { "tag:carcommand-tesla": ["autogroup:admin"] },
   "nodeAttrs": [ { "target": ["tag:carcommand-tesla"], "attr": ["funnel"] } ]
   ```
   A rule with `"src": ["*"]` includes tagged nodes; narrow it to `autogroup:member`.
2. Make an auth key (Settings › Keys): not reusable, not ephemeral, pre-approved, tagged `tag:carcommand-tesla`. It is used once; after that the node's state signs it in.
3. Start the node and the receiver.
   - **One container:** recreate it with the key: `docker stop carcommand && docker rm carcommand`, then the run line again with `-e TS_AUTHKEY=tskey-auth-...`. Or keep the key off the run line: `docker exec -i carcommand sh -c 'umask 077; cat > /data/secrets/tailscale-authkey'`, paste the key, Ctrl-D, then `docker restart carcommand`. The site learns the node's name by itself once the node has its certificate (`TESLA_STREAM_HOST`, if you set it, must match).
   - **Compose:** `scripts/tesla-net-key.sh`, run from the checkout, asks for the key without echoing it and keeps it in `stack/secrets/tailscale-authkey`, out of `.env` and the compose file (`make tesla-net-key` runs it over ssh). Set `TESLA_STREAM_HOST=carcommand-stream.<your tailnet>.ts.net` in `stack/.env`, start the two containers with `docker compose -f stack/docker-compose.yml --profile tesla up -d --no-deps tesla-net tesla-telemetry`, and recreate the site so it reads the new value: `docker compose -f stack/docker-compose.yml up -d --no-deps web`. Over ssh, `make tesla-up` and `make web` do the same.

   Your tailnet's name is on the DNS page of the admin console. If a node named `carcommand-stream` is already on the tailnet, Tailscale names the new one `carcommand-stream-1`: remove the old node first.
4. Once the node serves its certificate (step 5's receiver line goes green, or the logs say the certificate was written), add `https://carcommand-stream.<your tailnet>.ts.net` to the app's allowed origins at developer.tesla.com. Tesla lets a car stream only to a domain the app has registered, and refuses domains containing "tesla", hence the node's name.
5. On Connect Tesla, step 5: **Register the stream address**, then **Start** each car. A car takes its config the next time it wakes; the page shows that, its last message, and any errors Tesla or the car reports.

## Running it

The receiver's Let's Encrypt certificate renews itself, and the receiver is restarted on the new one.

One container: `docker exec carcommand carcommand status` lists `tesla-net` and `receiver`; their lines are in `docker logs carcommand` and in `/data/logs/tesla-net.log` and `receiver.log`. To turn the stream off, delete `/data/tesla/net` in the container (`docker exec carcommand rm -r /data/tesla/net`), recreate the container without the key, and remove the node in the Tailscale admin console.

Compose: `docker compose -f stack/docker-compose.yml --profile tesla logs --tail 100 tesla-net tesla-telemetry` shows both containers; `docker compose -f stack/docker-compose.yml --profile tesla restart tesla-net` restarts them (the receiver follows the node). Over ssh: `make tesla-logs` and `make tesla-restart`.

Raw records are kept 120 days in `cc_tesla_raw`, so a fix can be replayed over them. While TeslaFi or TeslaMate writes a car's history, the stream records beside it. Connect Tesla compares the two over the last week and lets you choose which one writes each car; the switch can be undone.

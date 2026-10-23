CONNECTOR ?= $(HOME)/Repos/CarConnectivity-connector-lucid
-include Makefile.local            # your host and car: MINI, TM_VIN, TM_NAME, TM_MODEL, TM_CAPACITY (untracked)
MINI      ?= user@your-docker-host
DEST      ?= ~/docker/carcommand/
TM_DB     ?= teslamate-db
TM_DB_USER ?= teslamate
TM_MAKE   ?= Tesla

.PHONY: setup backup restore staging wheel deploy web logs test refacts secret-key tesla-net-key tesla-up tesla-restart tesla-logs connector-lock one-container one-container-run one-container-demo one-container-multiarch one-container-move media signin-reset setup-code elevation-fit

# First run: stack/.env with generated passwords, and stack/secrets/. Refuses to
# overwrite an existing .env, which holds the password to the database volume.
setup:
	./scripts/setup.sh

# Dump the database to ./backups (or BACKUP_DIR). Safe while everything is running.
backup:
	./scripts/backup.sh $(BACKUP_DIR)

# Put a dump back: make restore FILE=backups/carcommand-....dump [ARGS=--force]
# Refuses to overwrite a database that already holds drives unless forced.
restore:
	./scripts/restore.sh $(FILE) $(ARGS)

# A second stack on the host, holding a restored copy of the live data, to try a
# deploy against before the real one. Schema and backfill problems show up here first.
#   make staging          bring it up from the newest dump and check every page
#   make staging ARGS=down   remove it
staging:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin ./scripts/staging.sh $(or $(ARGS),up)'

# A wheel of the connector checkout at CONNECTOR, for CC_CONNECTORS to pin before it is on PyPI;
# VERSION=0.4.0.dev1 names it, otherwise the checkout's tag does.
wheel:
	$(RM) stack/cc/wheels/*.whl
	cd $(CONNECTOR) && $(RM) -r dist && $(if $(VERSION),SETUPTOOLS_SCM_PRETEND_VERSION=$(VERSION)) .venv/bin/python -m build --wheel -o $(CURDIR)/stack/cc/wheels
	ls -1 stack/cc/wheels

# Never ships .env or secrets/: those exist only on the host that runs the stack.
#
# The protect filters matter as much as the excludes. --delete removes anything at the
# destination that is not in the source, and --exclude .env only spares a file named
# exactly that: a .env backup, an operator's compose override or a host-only note was
# deleted on the next deploy. `P` protects a destination file from --delete without
# stopping the tracked .env.example from being copied.
PROTECT = --filter='P .env*' --filter='P *.env' --filter='P secrets/***' --filter='P docker-compose.override.yml' --filter='P *.bak*' --filter='P *.local' --filter='P backups/***'
SYNC    = rsync -av --delete --exclude .env --exclude secrets/ --exclude __pycache__ --exclude .pytest_cache $(PROTECT)

# Both targets ship the same two things. `web` used to ship only stack/, and since SYNC
# carries --delete that quietly removed scripts/ from the host on every site deploy —
# the third time --delete has eaten something nobody asked it to.
DEPLOY = $(SYNC) $(1) stack/ $(MINI):$(DEST) && rsync -a scripts/ $(MINI):$(DEST)scripts/

# Both images ship connectors.json; catalogue-sync.sh rebuilds whichever runs another copy.
deploy:
	$(call DEPLOY,)
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin ./scripts/catalogue-sync.sh'

# `make wheel` is for developing the connector against a local checkout; production installs the released package.
# Rebuild and restart only the web container. --no-deps matters: without it compose also recreates cc
# (its dependency) whenever the shared .env changed, and the connector loses its live tree for a minute.
secret-key:  ## add CC_SECRET_KEY to the host's .env (keeps a copy; does nothing when one is set)
	ssh $(MINI) 'cd $(DEST) && ./scripts/secret-key.sh'

connector-lock:  ## pin every other-brand connector wheel by sha256 (stack/shared/connectors.json), checked against PyPI, inside the connector image
	cd stack && docker compose build cc
	docker run --rm --user 0 -v "$(CURDIR):/repo" -w /repo --entrypoint python3 carcommand-cc -s scripts/connector-lock.py

lucid-login:  ## mint the Lucid refresh token into stack/secrets/lucid-token.json (asks for username and password once)
	mkdir -p stack/secrets && chmod 700 stack/secrets
	cd stack && docker compose run --rm -it --no-deps -v "$$(pwd)/secrets:/out" cc python3 /app/lucid_login.py /out/lucid-token.json

# Tesla's live stream (docs/tesla-stream.md). The auth key is typed on the host, unseen,
# into secrets/tailscale-authkey; it never passes through this machine's disk or the chat.
tesla-net-key:  ## keep the Funnel node's Tailscale auth key on the host (asks once, not echoed)
	rsync -a scripts/ $(MINI):$(DEST)scripts/
	ssh -t $(MINI) 'cd $(DEST) && ./scripts/tesla-net-key.sh'

tesla-up:  ## start (or update) the Funnel node and the receiver; the site and the connector are left alone
	$(call DEPLOY,--exclude cc/ --exclude web/)
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose --profile tesla up -d --no-deps tesla-net tesla-telemetry'

tesla-restart:  ## restart the Funnel node; the receiver follows it
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose --profile tesla restart tesla-net'

tesla-logs:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose --profile tesla logs --tail 100 tesla-net tesla-telemetry'

web:
	$(call DEPLOY,--exclude cc/)
	@ssh $(MINI) 'cd $(DEST) && cmp -s shared/connectors.json cc/connectors.json' || { echo "connectors.json changed: the connector's files go too"; $(call DEPLOY,); }
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose up -d --build --no-deps web && sleep 6 && curl -s -o /dev/null -w "web %{http_code}\n" http://localhost:3044/health'
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin ./scripts/catalogue-sync.sh'

signin-reset:  ## locked out? forget the sign-in switch chosen in Settings and the Cloudflare Access pin (the password is kept), then restart the site
	ssh $(MINI) 'cd $(DEST) && export PATH=$$PATH:/usr/local/bin && docker compose exec -T web python3 signin_reset.py && docker compose restart web'

elevation-fit:  ## fit the Lucid's raw GPS height to metres, against the Teslas on shared roads and the terrain model (a check, changes nothing)
	ssh $(MINI) 'cd $(DEST) && export PATH=$$PATH:/usr/local/bin && docker compose exec -T web python3 elevfit.py'

setup-code:  ## print the setup code: the first password needs it after the first hour, and turning sign-in on always does
	ssh $(MINI) 'cd $(DEST) && export PATH=$$PATH:/usr/local/bin && docker compose exec -T web python3 setup_code.py'

logs:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose logs --tail 200 -f cc'

test:
	cd stack/cc && python3 -m pytest -q

# Load a TeslaFi export (plain or .gz): make import FILE=~/Downloads/TeslaFiData.csv.gz [ARGS=--dry-run]
media:  ## re-record the README loop, social card and every screenshot from the demo car on a throwaway local site (before each release)
	brand/make-demo-media.sh

demo:  ## six weeks of a fictional car around Springfield, generated and imported: a first look at an empty install
	cd stack && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 demo.py /tmp/TeslaFiData-demo.csv.gz
	cd stack && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 -c "import app, demo; demo.prepare(app)"
	cd stack && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 teslafi_load.py /tmp/TeslaFiData-demo.csv.gz

# The one container (stack/aio): Postgres, the site and the connector in a single image.
#   make one-container            build it for this machine as $(IMAGE)
#   make one-container-run        run it on port 3044 with its data in the volume carcommand
#   make one-container-demo       the demo car, inside it
#   make one-container-multiarch  build amd64 and arm64 with buildx (needs QEMU for the other one)
#   make one-container-move       move this checkout's compose install into it (docs/install.md)
IMAGE  ?= carcommand:local
VOLUME ?= carcommand
ONE_BUILD_ARGS = $(if $(CC_CONNECTORS),--build-arg CC_CONNECTORS='$(CC_CONNECTORS)')
one-container:
	DOCKER_BUILDKIT=1 docker build -f stack/aio/Dockerfile $(ONE_BUILD_ARGS) -t $(IMAGE) stack

one-container-run:
	docker run -d --name carcommand --restart unless-stopped --stop-timeout 30 -p 3044:3044 -v $(VOLUME):/data $(IMAGE)

one-container-demo:
	docker exec carcommand carcommand demo

one-container-multiarch:
	docker buildx build --platform linux/amd64,linux/arm64 -f stack/aio/Dockerfile $(ONE_BUILD_ARGS) -t $(IMAGE) stack

one-container-move:
	./scripts/move-to-one-container.sh $(IMAGE) $(VOLUME)

# Run the working-out again over the raw feed rows from a date, after a fix: make replay VIN=5YJ... FROM=2026-09-04
replay:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 teslafi_live.py $(VIN) $(FROM)'

# Work drive and charge facts out again where they disagree with the readings, after a fix: make refacts FROM=2026-09-04 [VIN=...] [ARGS=--dry-run]
refacts:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 refacts.py $(FROM) $(if $(VIN),--vin $(VIN)) $(ARGS)'

# Work a connector car's drives out again from its states and readings (the site also does this in the background for the last three days): make rebuild-drives FROM=2026-09-04 [VIN=...] [ARGS=--dry-run]
rebuild-drives:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 driverebuild.py $(FROM) $(if $(VIN),--vin $(VIN)) $(ARGS)'

# Fill a connector car's charges that have an end and no start, from its CHARGING states: make charge-starts FROM=2026-09-28 [VIN=...] [ARGS=--dry-run]
charge-starts:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 chargestart.py $(FROM) $(if $(VIN),--vin $(VIN)) $(ARGS)'

# Fill a hole from TeslaFi's drive and charge summaries (no routes or curves; the export has those): make teslafi-history VIN=5YJ... FROM=2026-09-04 [TO=2026-09-28]
teslafi-history:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 teslafi_history.py $(VIN) $(FROM) $(TO)'

# The connector's tree for a car as it stood at a moment: make raw VIN=... [AT=2026-09-28T10:00]
raw:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 raw.py $(VIN) $(AT)'

import:
	scp -q "$(FILE)" $(MINI):/tmp/$(notdir $(FILE))
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose cp /tmp/$(notdir $(FILE)) web:/tmp/$(notdir $(FILE)) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 teslafi_load.py /tmp/$(notdir $(FILE)) $(ARGS)'

# Name the places in the history: cluster trip/charge endpoints, reverse-geocode each cluster once (Nominatim, 1/s).
discover:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 discover.py $(ARGS)'

# How well local maps names real stops: 20 of the busiest cells the connector named in the last
# year, its public name next to local maps' own answer, never a coordinate. Prints and exits, for a person to mark up.
maps-check:
	ssh $(MINI) 'cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 maps_check.py'

# A TeslaMate history into Car Command, cut where the live connector took over.
# Dumps six tables read-only from TeslaMate's database container (TM_DB, user and database TM_DB_USER),
# copies them into web, loads. ARGS=--dry-run to look first.
import-teslamate:
	ssh $(MINI) 'set -e; mkdir -p /tmp/tm; for t in positions drives charging_processes charges states updates; do PATH=$$PATH:/usr/local/bin docker exec $(TM_DB) psql -U $(TM_DB_USER) -d $(TM_DB_USER) -At -c "\\copy (select * from $$t) to stdout with csv header" > /tmp/tm/$$t.csv; done; cd $(DEST) && PATH=$$PATH:/usr/local/bin docker compose cp /tmp/tm web:/tmp/tm && PATH=$$PATH:/usr/local/bin docker compose exec -T web python3 teslamate_load.py /tmp/tm --vin $(TM_VIN) --name $(TM_NAME) --model $(TM_MODEL) --capacity $(TM_CAPACITY) --manufacturer $(TM_MAKE) $(ARGS)'

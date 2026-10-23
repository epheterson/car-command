# syntax=docker/dockerfile:1
# Car Command in one container: Postgres 18, the site, the connector and the schema step,
# run by stack/aio/carcommand.py. Built from stack/ (the context), from the same sources as
# the compose images:
#
#   docker build -f stack/aio/Dockerfile -t carcommand stack
#   docker run -d --name carcommand --stop-timeout 30 -p 3044:3044 -v carcommand:/data carcommand
#
# Dockerfile.dockerignore beside this keeps .env, secrets/ and the tests out of the context.

# Tesla's live stream, which runs only when given a Tailscale auth key (docs/tesla-stream.md):
# the binaries of the two images compose runs for it, the same tags and digests (a test holds
# the two files to that), without their debug symbols, which the programs never read.
FROM tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065 AS tailscale
FROM tesla/fleet-telemetry:v0.9.5@sha256:7cb5f429210128b8edc5aac1e403589315893106f4c5a3ff85cb32213e2f8e37 AS telemetry
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f AS stream
RUN apt-get update && apt-get install -y --no-install-recommends binutils && rm -rf /var/lib/apt/lists/*
COPY --from=tailscale /usr/local/bin/containerboot /usr/local/bin/tailscale /usr/local/bin/tailscaled /out/
COPY --from=telemetry /fleet-telemetry /out/
RUN strip /out/*

# python:3.12-slim, pinned by digest; Dependabot (docker) moves the digest with the tag.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

ARG PG_MAJOR=18
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 PATH=/usr/lib/postgresql/18/bin:$PATH

# Fixed ids, so a volume's owners mean the same thing in every build: postgres as in the
# official image, carcommand and web as in the connector's and the site's images; tsnet (the
# Funnel node) and telemetry (Tesla's receiver) for the stream.
RUN groupadd -r -g 999 postgres && useradd -r -u 999 -g 999 -d /var/lib/postgresql -s /bin/sh postgres \
 && groupadd -r -g 10001 carcommand && useradd -r -u 10001 -g 10001 -d /tmp carcommand \
 && groupadd -r -g 10002 web && useradd -r -u 10002 -g 10002 -d /tmp web \
 && groupadd -r -g 10003 tsnet && useradd -r -u 10003 -g 10003 -d /tmp tsnet \
 && groupadd -r -g 10004 telemetry && useradd -r -u 10004 -g 10004 -d /tmp telemetry \
 && groupadd -r -g 10005 teslaproxy && useradd -r -u 10005 -g 10005 -d /tmp teslaproxy

# git: CC_CONNECTORS may name a connector by git ref. locales: CarConnectivity converts units by
# locale (en_US), and the database is created in it. tini: PID 1, so orphans are reaped.
# Postgres from its own Debian repository; no cluster is made at install, the first start makes one in /data.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git locales tini \
 && sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && mkdir -p /etc/postgresql-common && echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.conf \
 && apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}" \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/* /var/lib/postgresql/* \
 && postgres --version

# The connector, as stack/cc/Dockerfile installs it: the system Python, so other brands'
# connectors can be added later with pip --user.
WORKDIR /app
COPY cc/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# The same build argument as the connector image, and the same local wheels; see CC_CONNECTORS
# in stack/.env.example.
ARG CC_CONNECTORS="carconnectivity-connector-lucid==0.4.0"
COPY cc/wheels/ /tmp/wheels/
RUN printf '%s\n' "$CC_CONNECTORS" | tr ';' '\n' | sed 's/^ *//; /^$/d' > /tmp/connectors.txt \
 && pip install --no-cache-dir --find-links /tmp/wheels -r /tmp/connectors.txt && rm -rf /tmp/connectors.txt /tmp/wheels

# The site, in its own environment: its pins and the connector's never meet.
COPY web/requirements.txt /srv/web/requirements.txt
RUN python3 -m venv /opt/web && /opt/web/bin/pip install --no-cache-dir -r /srv/web/requirements.txt

# The stream's programs and settings. The build fails here if one cannot run on this base.
COPY --from=stream /out/ /usr/local/bin/
COPY tesla/net.sh /srv/tesla/net.sh
COPY aio/tesla-serve.json /srv/tesla/serve.json
COPY aio/tesla-telemetry.json /srv/tesla/telemetry.json
RUN tailscale version >/dev/null && tailscaled --version >/dev/null \
 && if ldd /usr/local/bin/fleet-telemetry | grep 'not found'; then exit 1; fi

# Tesla's signing proxy (teslamotors/vehicle-command 0.4.1), the binary from Tesla's own image,
# pinned by digest. It runs only when a Tesla app is configured (carcommand.py, tesla-proxy).
COPY --from=tesla/vehicle-command:0.4.1@sha256:e8adac679bf5e72790c588e620bb4a7992a8f80d4c3bd98da6d1129f59daf482 /usr/local/bin/tesla-http-proxy /usr/local/bin/tesla-http-proxy

COPY cc/ /app/
COPY config/ /config/
COPY web/ /srv/web/
COPY aio/carcommand.py /srv/aio/carcommand.py
# One catalogue and one sealing module for both, from stack/shared, whatever copies the two
# directories carry.
COPY shared/connectors.json shared/sealing.py /app/
COPY shared/connectors.json shared/sealing.py /srv/web/
# Fail the build, not the first request, on a template syntax error (as the site's image does).
RUN cd /srv/web && /opt/web/bin/python -c "import filters; from jinja2 import Environment, FileSystemLoader; e = Environment(loader=FileSystemLoader('templates')); filters.register(e); [e.get_template(t) for t in e.list_templates()]" \
 && chmod +x /app/entrypoint.sh /srv/aio/carcommand.py \
 # the wheels came in with cc/ and were installed above
 && rm -rf /app/wheels \
 && ln -s /srv/aio/carcommand.py /usr/local/bin/carcommand \
 # The connector reads secrets/lucid-token.json at /secrets in compose; here it is in the volume.
 && ln -s /data/connector /secrets \
 && python3 -m compileall -q /app /srv/web /srv/aio >/dev/null

WORKDIR /srv/web
VOLUME /data
EXPOSE 3044
# Run with --stop-timeout 30: the site and the connector get 6 s, then Postgres up to 20 for a
# clean shutdown. Docker's default 10 s is usually enough; past it, Postgres replays its log on the next start.
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 CMD ["carcommand", "health"]
ENTRYPOINT ["/usr/bin/tini", "--", "carcommand"]
CMD ["start"]

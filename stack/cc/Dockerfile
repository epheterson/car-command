# python:3.12-slim, pinned by digest; Dependabot (docker) moves the digest with the tag.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

# git: CC_CONNECTORS may name a connector by git ref.
# locales: CarConnectivity converts units by locale; we want en_US (miles).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git locales \
 && sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
 && locale-gen \
 && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Which CarConnectivity connectors this image carries: pip specs, separated by semicolons
# or newlines, from CC_CONNECTORS in stack/.env. Any brand CarConnectivity supports; the
# default is the Lucid connector at its pinned PyPI release.
ARG CC_CONNECTORS="carconnectivity-connector-lucid==0.1.8"
RUN printf '%s\n' "$CC_CONNECTORS" | tr ';' '\n' | sed 's/^ *//; /^$/d' > /tmp/connectors.txt \
 && pip install --no-cache-dir -r /tmp/connectors.txt && rm /tmp/connectors.txt

COPY render_config.py entrypoint.sh healthcheck.py init_db.py lucid_login.py supervise.py plugins.py plugin_role.py privsep.py sealing.py privacy.py logscrub.py sitecustomize.py connectors.json ./
# sitecustomize.py rounds the positions the connector sends to Nominatim (privacy.py); it
# loads only from a directory on the path.
ENV PYTHONPATH=/app
RUN chmod +x entrypoint.sh

# Two users. The supervisor runs as root, only to read the vault key and the passwords the
# entrypoint moves into root-only files, and to write CarConnectivity's config. Everything
# that runs code from elsewhere (CarConnectivity, its connectors and plugins, pip, the
# import check of a new connector) runs as `carcommand`, without them (privsep.py).
RUN groupadd -r -g 10001 carcommand && useradd -r -u 10001 -g 10001 carcommand
# Other brands added from Settings install here (pip --user, plugins.py), on a volume so they
# survive restarts and rebuilds. Owned by the user before the volume first mounts, so Docker
# copies that ownership into it. Root's Python never reads it: PYTHONNOUSERSITE here, and
# python3 -s for the supervisor; the user's processes are given PYTHONUSERBASE without it.
ENV PYTHONUSERBASE=/plugins PYTHONNOUSERSITE=1
RUN mkdir -p /plugins && chown carcommand:carcommand /plugins

EXPOSE 4000 40000
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 CMD ["python3", "-s", "/app/healthcheck.py"]
ENTRYPOINT ["/app/entrypoint.sh"]

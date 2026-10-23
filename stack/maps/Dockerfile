# Local maps: OpenStreetMap regions downloaded as drives need them, turned into Valhalla
# tiles and a small place index, answering place lookups so no position leaves the box.
# ghcr.io/valhalla/valhalla:3.9.0, pinned by digest; Dependabot (docker) moves the digest with the tag.
FROM ghcr.io/valhalla/valhalla:3.9.1@sha256:4fd114eb1d26b8fc2f43bcb3df234c1b63a0bf13c277405e5943d5361dc575de
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends osmium-tool python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py regions.py state.py builder.py placeindex.py reverse.py geo.py match.py ./
RUN useradd -r -u 10002 maps && mkdir -p /data && chown maps /data
USER maps
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/status', timeout=4)"]
ENTRYPOINT ["python3", "/app/server.py"]

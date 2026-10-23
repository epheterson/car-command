# python:3.12-slim, pinned by digest; Dependabot (docker) moves the digest with the tag.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py auth.py units.py charts.py filters.py clockfmt.py mapsoffer.py recorder.py places.py placeicons.py notify.py cost.py stats.py facts.py discover.py day.py history.py teslamate_import.py teslamate_load.py teslafi_live.py service.py settings.py teslafi_import.py teslafi_load.py summary.py samples.py runs.py thumbs.py weather.py manual.py autotag.py teslamate_live.py demo.py health.py raw.py teslafi_history.py nav.py nowpage.py chartdata.py daypage.py statspage.py placespage.py settingsform.py planview.py sources.py tesla_fleet.py tesla_keys.py teslapage.py teslaapp.py zonelist.py livestream.py connect.py proxyauth.py signin_reset.py setup_code.py tesla_stream.py tesla_jws.py tesla_net.py tesla_live.py tesla_nav.py mapsclient.py mapsworker.py routematch.py maps_check.py averages.py regions.py placeprice.py billed.py urdb.py esrikey.py alertchans.py carlinks.py carnames.py carspage.py adminapi.py connectors.py connectors.json demo_routes.json sealing.py history_writer.py tesla_compare.py writerswitch.py identity.py activity.py version.py energy.py energy_cloud.py energypage.py commands.py commandcheck.py expected.py tesla_cmd.py tesla_proxy.py charge_hold.py strips.py neighbours.py records.py effcompare.py effchart.py lifetime.py softwarepage.py otahistory.py toproutes.py calview.py tiredays.py healthlog.py elevfit.py driveview.py parkedview.py billread.py terrain.py fleet_usage.py mcpserver.py mcptools.py driveedits.py driverebuild.py chargestart.py refacts.py reach.py garagedoor.py garageopen.py garageclose.py garagelog.py feed_gaps.py importjob.py importroutes.py devices.py ./
COPY templates/ templates/
COPY static/ static/
# The region outlines and the average prices current when the image was built; a newer
# copy of the prices is downloaded at run time and kept in the database.
COPY rates/ rates/
# Fail the build, not the first request, on a template syntax error.
RUN python -c "import filters; from jinja2 import Environment, FileSystemLoader; e = Environment(loader=FileSystemLoader('templates')); filters.register(e); [e.get_template(t) for t in e.list_templates()]"
# /run/tesla-proxy is where the site writes the signing proxy's files; a new volume there takes this owner.
RUN useradd -r -u 10002 web && install -d -o 10002 -g 10002 -m 0700 /run/tesla-proxy && install -d -o 10002 -g 10002 /data/terrain /data/imports
USER web
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=8s --start-period=20s --retries=3 CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=6).status == 200 else 1)"]
# app:server applies the proxy headers itself, after keeping the socket's own address for
# the admin API's network rule; uvicorn must not rewrite it first.
CMD ["uvicorn", "app:server", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

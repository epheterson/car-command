# Local maps (optional)

Place and street names can come from a public geocoder you turn on. This add-on does the lookup on your own box instead, from OpenStreetMap data, so a stop's coordinate stays home.

## Turning it on

```sh
docker compose -f stack/docker-compose.yml --profile maps up -d maps
```

Set `MAPS_URL=http://maps:8090` in `stack/.env` and recreate the site so it reads it: `docker compose -f stack/docker-compose.yml up -d --no-deps web` (a plain restart keeps the old environment). It is off by default, and the rest of the stack works the same without it.

## How it gets its maps

It downloads regions as your cars need them: the first drive into a new area queues that area's OpenStreetMap extract from Geofabrik, one at a time, and builds a local map and place index from it. Regions are capped at about 700 MB of source data, so a big state is split (California comes as NorCal and SoCal) and a small one comes whole. Settings › Maps shows the queue and what is built, and adds or removes a region by hand. `make maps-check` (over ssh; locally `docker compose -f stack/docker-compose.yml exec -T web python3 maps_check.py`) compares local names with public ones for the busiest stops, without printing a coordinate.

## Drives drawn on the roads

A car that reports every 15 to 60 s (a Lucid, a TeslaFi history) draws a drive as straight lines that cut corners. With local maps built, drives whose positions are more than 8 s apart at the median are matched to roads in the background by the add-on, newest first, and the matched line is kept beside the drive. A Tesla streaming every second is left alone. The line runs from where the car was parked either side, when that is within 300 m of its first and last readings.

The car's readings are not changed, and distance, speed and efficiency still come from them and the odometer. A matched line that disagrees with the car (over 1.3 or under 0.7 times the odometer's distance, or with few readings placed on a road) is not drawn, and the reason is recorded. The drive page can lay the readings over the matched line. A new build (a region added, a monthly refresh) matches every drive again. Matching runs on the add-on; a drive outside your regions keeps its readings.

## Sizing

Measured on an M4 Mac mini with four regions (Northern and Southern California, Oregon, Nevada: 1.7 GB of downloads):

| | |
|---|---|
| Build time | about 10 minutes |
| Disk, built | 5.0 GB |
| RAM while answering | about 0.9 GB |
| Free disk to build | about three times the downloads, plus 1 GB (a build won't start without it) |

A Pi 5 with an SSD is a sensible floor; a Pi 4 with 4 GB suits one state-sized region. More regions need more disk, not more RAM, since one build runs at a time.

## The two public switches

Both are on Settings › Maps, and both are off on a new install.

- **Satellite close-ups** (off by default; needs your own ArcGIS API key). On, the drive page's start and end close-ups and the parked and charge map strips show Esri World Imagery, with Esri's credit ("Powered by Esri", a tap opens the full source line). The route map and Now keep the background above. Your browser asks `ibasemaps-api.arcgis.com` for the tiles of the area you view with your key on each request, so Esri sees the viewed area and your key. The key is sealed in the database like the other secrets, only its last four are shown back, and Remove switches satellite off with it. The switch stays disabled until a key is saved; Settings › Maps links to Esri's free sign-up (location.arcgis.com/sign-up). Scope the key to basemap tiles.
  Make the key for basemap tiles only, and in the ArcGIS dashboard restrict it to your site's address (an allowed referrer): it sits in your browser and in Esri's logs, so a key copied elsewhere is then useless.
  Why a key: the World Imagery item's licence reads "This work is licensed under the Esri Master License Agreement", and Esri's own Leaflet library says "If you're using Esri content and services, you'll need to license your usage with an API key or an ArcGIS identity". The keyless tile endpoint answers, but nothing in the terms allows it, so Car Command does not use it. The credit text Esri gives for the service is "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community". The free tier covers far more tiles than one garage views (a few tiles a page, cached by the browser). Esri's attribution page was not readable when this was written, so "Powered by Esri" follows Esri's published pattern rather than a quotation; re-check it before any public release.
- **Map backgrounds.** On, OpenFreeMap's public tile server sees the area you are viewing and draws the background. Off, routes and places draw on a plain background; local maps doesn't build backgrounds yet.
- **Public geocoder.** On, a stop outside every region you've built falls back to OpenStreetMap's public Nominatim, which sees that stop rounded to about 100 m. Off, the stop stays unnamed until local maps covers it or you name it. Without local maps, this is the only source of names.

A name you set or edited by hand is kept: nothing automatic renames it.

## What still leaves the box

With both switches off, Geofabrik sees which regions you download (plain file requests, no coordinates). With tiles on, OpenFreeMap sees the map view; with the geocoder on, Nominatim sees the stops it is asked to name.

Place names and map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), under the Open Database License. Built on [Valhalla](https://github.com/valhalla/valhalla) and [osmium](https://osmcode.org/osmium-tool/).

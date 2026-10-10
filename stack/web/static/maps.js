// Shared map parts: a vector base in the site's light or dark look, routes with a casing so they read
// on any base, charge pins sized by energy, and the car colours from the stylesheet.
(function () {
  function dark() { var t = document.documentElement.getAttribute("data-theme"); if (t === "dark") return true; if (t === "light") return false; return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches); }
  var css = getComputedStyle(document.documentElement);
  var fallback = ["#A87F12", "#8A5FD6", "#D6605C", "#2F8F8F", "#B06AA0", "#8C7A3E", "#C2410C", "#D9467A", "#6B8526", "#56708F", "#2A4E90", "#306048", "#8A8A7E", "#41464D"];
  var palette = fallback.map(function (c, i) { return (css.getPropertyValue("--c" + i) || "").trim() || c; });
  var STYLE = { light: "https://tiles.openfreemap.org/styles/positron", dark: "https://tiles.openfreemap.org/styles/dark" };  // free, no key
  var link = function (href, text) { return '<a href="' + href + '" target="_blank" rel="noopener">' + text + "</a>"; };
  var CREDIT = link("https://openfreemap.org", "OpenFreeMap") + " " + link("https://www.openmaptiles.org/", "&copy; OpenMapTiles") + " Data from " + link("https://www.openstreetmap.org/copyright", "OpenStreetMap");
  // The public tile services are opt-in (Settings > Maps); off by default they'd send every
  // viewed area to OpenFreeMap or OSM. With them off, a map draws routes and pins on the
  // page's own background instead of fetching a base at all.
  function tilesOn() { return document.body.dataset.tiles === "public"; }
  var NO_TILES_NOTE = "Map backgrounds are off (Settings › Maps)";

  // The dark style is neutral black; its ground, water and roads take the site's dark tones instead,
  // so the map sits in its card rather than reading as a hole in the page. Roads are lighter than ground.
  var DARK = {
    fill: { background: "#1D1C24", landcover_ice_shelf: "#1D1C24", landcover_glacier: "#1D1C24", road_area_pier: "#1D1C24",
            landuse_residential: "#211F29", landcover_wood: "#22222A", landuse_park: "#22222A", building: "#27252F",
            "aeroway-area": "#19181F", water: "#141319" },
    line: { waterway: "#141319", highway_path: "#24222C", highway_minor: "#27252F", highway_major_casing: "#141319",
            highway_major_inner: "#35333E", highway_major_subtle: "#2C2A35", highway_motorway_casing: "#141319",
            highway_motorway_inner: "#46434F", highway_motorway_subtle: "#2C2A35", road_pier: "#1D1C24",
            railway: "#2C2A35", railway_minor: "#2C2A35", railway_transit: "#2C2A35", railway_dashline: "#1D1C24",
            railway_minor_dashline: "#1D1C24", railway_transit_dashline: "#1D1C24", "aeroway-taxiway": "#27252F",
            "aeroway-runway": "#2C2A35", "aeroway-runway-casing": "#1D1C24", boundary_state: "#3A3846",
            "boundary_country_z0-4": "#3A3846", "boundary_country_z5-": "#3A3846" }
  };
  function tone(gl) {
    gl.once("style.load", function () {
      function paint(id, prop, colour) { if (gl.getLayer(id)) gl.setPaintProperty(id, prop, colour); }
      Object.keys(DARK.fill).forEach(function (id) { paint(id, id === "background" ? "background-color" : "fill-color", DARK.fill[id]); });
      paint("building", "fill-outline-color", "#2C2A35");
      Object.keys(DARK.line).forEach(function (id) { paint(id, "line-color", DARK.line[id]); });
    });
  }

  // Vector maps need WebGL and the two libraries; without them the page gets greyed raster tiles instead.
  function vector() {
    if (!window.maplibregl || !window.L || !L.maplibreGL) return false;
    try { var c = document.createElement("canvas"); return !!(c.getContext("webgl2") || c.getContext("webgl")); } catch (e) { return false; }
  }
  function raster(map) {
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; " + link("https://www.openstreetmap.org/copyright", "OpenStreetMap") + " contributors" }).addTo(map);
    var box = map.getContainer(); box.classList.add("cc-quiet"); if (dark()) box.classList.add("cc-dark");
  }
  // A map that never moves keeps a picture of its base and gives its WebGL context back: a browser holds
  // about sixteen at once, and a page of small maps would lose the oldest.
  function keepStill(map, layer) {
    var gl = layer.getMaplibreMap();
    gl.once("idle", function () {
      var url;
      try { url = gl.getCanvas().toDataURL("image/png"); } catch (e) { return; }
      L.imageOverlay(url, layer.getBounds(), { pane: "tilePane", interactive: false }).addTo(map);
      map.removeLayer(layer);
    });
  }
  function base(el, opts) {
    opts = opts || {};
    var map = L.map(el, { zoomControl: opts.zoomControl !== false, scrollWheelZoom: !!opts.scrollWheelZoom, preferCanvas: !!opts.preferCanvas, dragging: opts.dragging !== false, attributionControl: opts.attributionControl !== false });
    // Paths added before the map has a view wait for it in order; the shared renderer goes first so it
    // is ready before any route or pin is drawn on it.
    map.getRenderer(L.polyline([]));
    if (!tilesOn()) {
      map.getContainer().classList.add("cc-no-tiles");
      if (map.attributionControl) map.attributionControl.setPrefix("").addAttribution(NO_TILES_NOTE + " · Names &copy; " + link("https://www.openstreetmap.org/copyright", "OpenStreetMap"));
      return map;
    }
    if (!vector()) { raster(map); return map; }
    var layer = L.maplibreGL({ style: dark() ? STYLE.dark : STYLE.light, attributionControl: { customAttribution: CREDIT }, canvasContextAttributes: { preserveDrawingBuffer: !!opts.still } }).addTo(map);
    map.whenReady(function () { if (dark()) tone(layer.getMaplibreMap()); if (opts.still) keepStill(map, layer); });
    return map;
  }
  function route(map, pts, colour, weight) {
    var w = weight || 3.5;
    return L.layerGroup([
      L.polyline(pts, { color: dark() ? "#141319" : "#FFFFFF", weight: w + 3, opacity: 0.9, interactive: false }),
      L.polyline(pts, { color: colour, weight: w, opacity: 0.95 })
    ]).addTo(map);
  }
  // A popup or tooltip body from lines of plain text: place names come from addresses and from what
  // people type, so they are never read as HTML. Empty lines are left out; a line may be a node
  // (a place(), below).
  function lines(list) {
    var box = document.createElement("div");
    list.filter(function (t) { return t !== null && t !== undefined && t !== ""; }).forEach(function (t, i) {
      if (i) box.appendChild(document.createElement("br"));
      box.appendChild(t instanceof Node ? t : document.createTextNode(String(t)));
    });
    return box;
  }
  // A place name in a popup that opens the naming card on its spot (shell.js, placecard.js).
  function place(name, lat, lon) {
    if (!name || lat == null || lon == null) return name;
    var b = document.createElement("button");
    b.type = "button";
    b.className = "pn";
    b.setAttribute("aria-haspopup", "dialog");
    b.dataset.lat = lat;
    b.dataset.lon = lon;
    b.textContent = name;
    return b;
  }
  function charge(map, p, colour) { return L.circleMarker([p.lat, p.lon], { radius: 5 + Math.min(9, Math.sqrt((p.kwh || 0) / 10)), color: p.fast ? palette[2] : (colour || palette[3]), weight: 2, fillOpacity: 0.55 }).addTo(map); }
  window.CCMap = { base: base, route: route, charge: charge, lines: lines, place: place, palette: palette, dark: dark, credit: CREDIT };
})();

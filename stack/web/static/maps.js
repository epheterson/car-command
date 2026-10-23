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
  // Satellite imagery is a second opt-in (Settings > Maps): Esri World Imagery, used only by the
  // street-level close-ups and strips (base(el, {close: true})). It needs the owner's own ArcGIS API
  // key, which the page hands over in <body data-sat-key>; the browser asks Esri for tiles with it,
  // so Esri sees the viewed area and the key.
  function satellite() { return document.body.dataset.sat === "on" && !!document.body.dataset.satKey; }
  var SAT = "https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
  // one line on a small map ("Powered by Esri"); a tap opens the full source line Esri asks for
  var SAT_CREDIT = '<span class="sat-c">Powered by ' + link("https://www.esri.com", "Esri") + '</span><span class="sat-x"> &mdash; Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community</span>';
  function imagery(map) {
    L.tileLayer(SAT + "?token=" + encodeURIComponent(document.body.dataset.satKey), { maxZoom: 19, attribution: SAT_CREDIT }).addTo(map);
    if (!map.attributionControl) oneLine(map, L.control.attribution({ prefix: false, position: "bottomright" }).addTo(map));  // a close-up asks for none; Esri's credit is required
    map.attributionControl && map.attributionControl.setPrefix("");
    map.getContainer().classList.add("cc-sat");
  }
  // Every map's credit is one line, cut short where the map is narrow (two small maps side by side on
  // a phone); a tap opens the whole of it.
  function oneLine(map, ctl) {
    var box = ctl.getContainer();
    // while it is cut short, a tap on a link opens the line rather than leaving the page
    box.addEventListener("click", function (e) {
      if (box.classList.contains("open")) { if (!e.target.closest("a")) box.classList.remove("open"); }
      else if (box.scrollWidth > box.clientWidth + 1 || !e.target.closest("a")) { e.preventDefault(); box.classList.add("open"); }
    });
    map.getContainer().classList.add("cc-credit");
  }
  var NO_TILES_NOTE = "Map backgrounds are off (Settings › Maps)";
  // On the big maps (opts.hint: Map, Places, Now) the plain background says so and offers the switch, so it
  // is not read as a map that failed to load.
  function offHint(map) {
    var box = L.DomUtil.create("div", "cc-off", map.getContainer());  // centred in the box by the stylesheet
    var m = document.querySelector('meta[name="cc-root"]');
    var say = document.createElement("span"), go = document.createElement("a");
    say.textContent = "Plain background";
    go.textContent = "Add a map"; go.href = ((m && m.content) || "") + "/settings/maps";
    box.appendChild(say); box.appendChild(go);
    L.DomEvent.disableClickPropagation(box);
  }

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
  // On a phone a map that takes every finger traps the page: scrolling past it drags the map instead. A
  // cooperative map leaves one finger to the page and gives two fingers the map (Leaflet's pinch handler
  // pans and zooms with two), and says so once, the first time a finger tries to drag it. The big Map
  // page opts out (opts.full): it is the page's whole job.
  var HINT_KEY = "cc-map-hint", HINT = "Use two fingers to move the map";
  function coarse() { return !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches); }
  function cooperative(map) {
    if (!coarse() || !map.dragging || !map.dragging.enabled()) return map;
    map.dragging.disable();
    var box = map.getContainer(), from = null, shown = false, note = null;
    try { shown = !!localStorage.getItem(HINT_KEY); } catch (e) {}
    function say() {
      if (shown) return;
      shown = true;
      try { localStorage.setItem(HINT_KEY, "1"); } catch (e) {}
      note = document.createElement("div");
      note.className = "cc-map-hint";
      note.setAttribute("role", "status");
      note.textContent = HINT;
      box.appendChild(note);
      setTimeout(function () { note.classList.add("on"); }, 20);
      setTimeout(function () { note.classList.remove("on"); setTimeout(function () { note.remove(); }, 300); }, 2000);
    }
    box.addEventListener("touchstart", function (e) { from = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null; }, { passive: true });
    box.addEventListener("touchmove", function (e) {
      if (from && e.touches.length === 1 && Math.hypot(e.touches[0].clientX - from.x, e.touches[0].clientY - from.y) > 8) say();
    }, { passive: true });
    ["touchend", "touchcancel"].forEach(function (t) { box.addEventListener(t, function () { from = null; }, { passive: true }); });
    return map;
  }
  function base(el, opts) {
    opts = opts || {};
    var map = L.map(el, { zoomControl: opts.zoomControl !== false, scrollWheelZoom: !!opts.scrollWheelZoom, preferCanvas: !!opts.preferCanvas, dragging: opts.dragging !== false, attributionControl: opts.attributionControl !== false, zoomSnap: opts.zoomSnap || 1 });
    if (!opts.full) cooperative(map);
    // Paths added before the map has a view wait for it in order; the shared renderer goes first so it
    // is ready before any route or pin is drawn on it.
    map.getRenderer(L.polyline([]));
    if (map.attributionControl) oneLine(map, map.attributionControl);
    if (opts.close && satellite()) { imagery(map); return map; }
    if (!tilesOn()) {
      map.getContainer().classList.add("cc-no-tiles");
      if (opts.hint) offHint(map);
      if (map.attributionControl) {
        if (opts.hint) map.attributionControl.remove();  // the card says it; nothing to credit under an empty box
        else map.attributionControl.setPrefix("").addAttribution(NO_TILES_NOTE + " · Names &copy; " + link("https://www.openstreetmap.org/copyright", "OpenStreetMap"));
      }
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
  function ends(map, a, b, colour) {
    if (a) L.circleMarker(a, { radius: 7, color: colour, fillColor: "#fff", fillOpacity: 1, weight: 3 }).addTo(map).bindTooltip("Start");
    if (b) L.circleMarker(b, { radius: 7, color: "#fff", fillColor: colour, fillOpacity: 1, weight: 3 }).addTo(map).bindTooltip("End");
  }
  // Start close-up, the route, end close-up: one row on a wide screen; on a phone the route
  // first and the two close-ups side by side under it (style.css, .trio). The close-ups are
  // still maps at street level that never move. o: {start: [lat, lon], route: [[lat, lon]...],
  // end, colour, zoom, line}; returns the three maps {start, route, end} (a close-up with no point
  // is null). `line(map, pts, colour, weight)` draws the route in place of the plain one (by speed).
  function trio(el, o) {
    el = typeof el === "string" ? document.getElementById(el) : el;
    var colour = o.colour || palette[0], pts = o.route || [], a = o.start || pts[0], b = o.end || pts[pts.length - 1], draw = o.line || route;
    el.classList.add("trio");
    function cell(cls, caption) {
      var f = document.createElement("figure"), m = document.createElement("div");
      f.className = "trio-" + cls;
      m.className = "map";
      f.appendChild(m);
      if (caption) { var c = document.createElement("figcaption"); c.textContent = caption; f.appendChild(c); }
      el.appendChild(f);
      return m;
    }
    var ca = cell("a", "Start"), cr = cell("r"), cb = cell("b", "End");
    if (pts.length < 2 && !a && !b) {  // nothing to place: three empty cells, no map
      [ca, cr, cb].forEach(function (c) { c.parentNode.classList.add("trio-none"); });
      return { route: null, start: null, end: null };
    }
    var out = { route: base(cr, { zoomControl: false }), start: null, end: null };
    if (pts.length >= 2) draw(out.route, pts, colour, 4); else if (a && b) L.polyline([a, b], { color: colour, weight: 3, dashArray: "6 8" }).addTo(out.route);
    ends(out.route, a, b, colour);
    out.route.fitBounds(pts.length >= 2 ? pts : [a || b, b || a], { padding: [24, 24], maxZoom: 16 });
    [["start", ca, a], ["end", cb, b]].forEach(function (c) {
      if (!c[2]) { c[1].parentNode.classList.add("trio-none"); return; }
      var m = base(c[1], { close: true, still: true, zoomControl: false, dragging: false, attributionControl: false });
      ["doubleClickZoom", "touchZoom", "boxZoom", "keyboard"].forEach(function (h) { if (m[h]) m[h].disable(); });
      if (pts.length >= 2) draw(m, pts, colour, 4);
      ends(m, c[0] === "start" ? a : null, c[0] === "end" ? b : null, colour);
      m.setView(c[2], o.zoom || 17);
      out[c[0]] = m;
    });
    return out;
  }
  var GLIDE_GAP = 120;  // seconds between two points past which the marker jumps rather than glides
  // A marker on `map` at where the car was at a time: `pts` are [lat, lon, t] in time order, and
  // between two of them the marker sits on the straight line from one to the next, as far along
  // as the time is, so it glides rather than hopping fix to fix; across a gap in the log longer
  // than GLIDE_GAP it jumps. Before the first or after the last it waits at that end. With a chart box it follows the chart's pointer (cc:chart-t) and
  // hides when the pointer leaves. Returns the marker, with marker.at(t) to move it by hand.
  function follow(map, pts, box, colour) {
    var mk = L.circleMarker(pts.length ? [pts[0][0], pts[0][1]] : [0, 0], { radius: 7, color: dark() ? "#141319" : "#FFFFFF", weight: 3, fillColor: colour || palette[0], fillOpacity: 1, interactive: false, className: "cc-follow" });
    function where(t) {
      var lo = 0, hi = pts.length - 1;
      while (lo < hi) { var m = (lo + hi) >> 1; if (pts[m][2] < t) lo = m + 1; else hi = m; }
      var b = pts[lo], a = lo > 0 ? pts[lo - 1] : b;
      if (t >= b[2] || b[2] <= a[2]) return [b[0], b[1]];
      // more than two minutes between them is a gap in the log, not a road: no line to glide along
      if (b[2] - a[2] > GLIDE_GAP) return t - a[2] < b[2] - t ? [a[0], a[1]] : [b[0], b[1]];
      var f = Math.max(0, (t - a[2]) / (b[2] - a[2]));
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    }
    mk.at = function (t) {
      if (t === null || t === undefined || !pts.length) { mk.remove(); return mk; }
      mk.setLatLng(where(t));
      if (!map.hasLayer(mk)) mk.addTo(map);
      return mk;
    };
    if (box) box.addEventListener("cc:chart-t", function (e) { mk.at(e.detail && e.detail.t); });
    return mk;
  }
  // Smooth motion for a live car: fixes arrive every few seconds, and the markers glide from where
  // they were drawn to each new one over the time the fixes were apart, so they move steadily
  // (a few seconds behind) instead of hopping. Across a gap (past LIVE_JUMP), in a hidden tab, or with
  // reduced motion asked for, they jump. The heading turns the short way round.
  // A leg is {from: [lat, lon, heading], to: [...], t0, ms} (heading may be null).
  function turn(a, b, f) {
    if (a === null || a === undefined || b === null || b === undefined) return b === undefined ? null : b;
    var d = ((b - a + 540) % 360) - 180;
    return (a + d * f + 360) % 360;
  }
  function legAt(leg, now) {
    var f = leg.ms > 0 ? Math.min(1, Math.max(0, (now - leg.t0) / leg.ms)) : 1, a = leg.from, b = leg.to;
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, turn(a[2], b[2], f)];
  }
  // how long a live glide from one fix to the next takes, in ms: the time between them up to
  // LIVE_LEG seconds (the car is shown a beat behind, not minutes), and none past LIVE_JUMP
  // seconds, where it is a gap and the marker jumps
  var LIVE_LEG = 6, LIVE_JUMP = 15;
  function legMs(gapSeconds) { return gapSeconds > 0 && gapSeconds <= LIVE_JUMP ? Math.min(gapSeconds, LIVE_LEG) * 1000 : 0; }
  function still() { return document.hidden || !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  // o.move(lat, lon, heading) draws the markers where they are now; returns {fix(lat, lon, heading)}.
  function glider(o) {
    var leg = null, at = null, last = null, frame = 0;
    function run(now) {
      var p = legAt(leg, now);
      at = p;
      o.move(p[0], p[1], p[2]);
      frame = now - leg.t0 < leg.ms ? requestAnimationFrame(run) : 0;
    }
    return {
      fix: function (lat, lon, heading) {
        var now = performance.now(), ms = last !== null ? legMs((now - last) / 1000) : 0;
        last = now;
        cancelAnimationFrame(frame);
        frame = 0;
        var from = at || [lat, lon, heading];
        if (!ms || still()) { at = [lat, lon, heading]; o.move(lat, lon, heading); return; }
        leg = { from: from, to: [lat, lon, heading], t0: now, ms: ms };
        frame = requestAnimationFrame(run);
      }
    };
  }
  window.CCMap = { base: base, cooperative: cooperative, route: route, charge: charge, lines: lines, place: place, palette: palette, dark: dark, credit: CREDIT, trio: trio, follow: follow, glideGap: GLIDE_GAP, satellite: satellite, glider: glider, legAt: legAt, legMs: legMs, turn: turn, still: still };
})();

// A heat layer drawn here, nothing fetched: each point [lat, lon, weight] a soft disc, the discs'
// shade adding up where they overlap, and the sum coloured along one ramp, from clear through
// `colour` to `deep` where the weight piles highest. Weights are on a log scale, so home does not
// leave every other place invisible. Redrawn whenever the map stops moving.
(function () {
  if (!window.L || !window.CCMap) return;
  function ramp(colour, deep) {
    var c = document.createElement("canvas"), x = c.getContext("2d"), g = x.createLinearGradient(0, 0, 256, 0);
    c.width = 256; c.height = 1;
    g.addColorStop(0, colour); g.addColorStop(0.55, colour); g.addColorStop(1, deep);
    x.fillStyle = g; x.fillRect(0, 0, 256, 1);
    var d = x.getImageData(0, 0, 256, 1).data;
    for (var i = 0; i < 256; i++) d[i * 4 + 3] = Math.round(Math.min(1, i / 200) * 0.88 * 255);
    return d;
  }
  var Heat = L.Layer.extend({
    initialize: function (pts, colour, deep) { this._pts = pts; this._ramp = ramp(colour, deep); },
    onAdd: function (map) {
      this._c = L.DomUtil.create("canvas", "cc-heat leaflet-zoom-hide");
      this._c.style.pointerEvents = "none";
      map.getPanes().overlayPane.appendChild(this._c);
      map.on("moveend resize", this._draw, this);
      this._draw();
    },
    onRemove: function (map) { map.off("moveend resize", this._draw, this); L.DomUtil.remove(this._c); },
    _draw: function () {
      var map = this._map, size = map.getSize(), c = this._c, x = c.getContext("2d"), max = 1;
      c.width = size.x; c.height = size.y;
      L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
      var r = Math.max(10, Math.min(34, map.getZoom() * 2.2));
      this._pts.forEach(function (p) { if (p[2] > max) max = p[2]; });
      this._pts.forEach(function (p) {
        var at = map.latLngToContainerPoint([p[0], p[1]]);
        if (at.x < -r || at.y < -r || at.x > size.x + r || at.y > size.y + r) return;
        var a = 0.2 + 0.8 * Math.log(1 + p[2]) / Math.log(1 + max), g = x.createRadialGradient(at.x, at.y, 0, at.x, at.y, r);
        g.addColorStop(0, "rgba(0,0,0," + a.toFixed(3) + ")"); g.addColorStop(1, "rgba(0,0,0,0)");
        x.fillStyle = g; x.fillRect(at.x - r, at.y - r, 2 * r, 2 * r);
      });
      if (!size.x || !size.y) return;
      var img = x.getImageData(0, 0, size.x, size.y), d = img.data, k = this._ramp;
      for (var i = 3; i < d.length; i += 4) {
        var s = d[i] * 4;
        if (!s) continue;
        d[i - 3] = k[s]; d[i - 2] = k[s + 1]; d[i - 1] = k[s + 2]; d[i] = k[s + 3];
      }
      x.putImageData(img, 0, 0);
    }
  });
  CCMap.heat = function (pts, colour, deep) { return new Heat(pts, colour, deep); };
})();

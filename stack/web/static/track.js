// A drive on its maps: the route coloured by speed, a marker on every map that follows the
// chart's pointer, the chart following the pointer along the route, and a replay that runs
// the marker and the chart's crosshair from start to end together. Needs maps.js and a chart
// box (chart.js) on the page. Points are [lat, lon, epoch seconds, speed in the page's units].
(function () {
  var css = getComputedStyle(document.documentElement);
  function tone(i) { return (css.getPropertyValue("--spd" + i) || "").trim() || "#7A52C7"; }
  function band(v, steps) { if (v === null || v === undefined) return null; var b = 0; for (var i = 1; i < steps.length; i++) if (v >= steps[i]) b = i; return b; }

  // the route as runs of one speed band, each its own line over one casing; a reading with no
  // speed keeps the band before it
  function painter(pts, steps) {
    return function (map, ll, colour, weight) {
      var w = weight || 4, runs = [], cur = null, last = 0;
      pts.forEach(function (p, i) {
        var b = band(p[3], steps);
        if (b === null) b = last;
        last = b;
        if (!cur || cur.b !== b) { cur = { b: b, ll: cur ? [cur.ll[cur.ll.length - 1]] : [] }; runs.push(cur); }
        cur.ll.push(ll[i]);
      });
      var layers = [L.polyline(ll, { color: CCMap.dark() ? "#141319" : "#FFFFFF", weight: w + 3, opacity: 0.9, interactive: false })];
      runs.forEach(function (r) { if (r.ll.length >= 2) layers.push(L.polyline(r.ll, { color: tone(r.b), weight: w, opacity: 1, interactive: false, lineCap: "round" })); });
      return L.layerGroup(layers).addTo(map);
    };
  }

  // a replay's clock past a gap in the log: inside a gap longer than the marker glides across,
  // straight on to the reading after it, so twenty unlogged minutes are not seconds of nothing
  function skip(pts, t, gap) {
    var lo = 0, hi = pts.length - 1;
    while (lo < hi) { var m = (lo + hi) >> 1; if (pts[m][2] <= t) lo = m + 1; else hi = m; }
    return lo > 0 && t < pts[lo][2] && pts[lo][2] - pts[lo - 1][2] > gap ? pts[lo][2] : t;
  }
  function nearestT(pts, t) {
    var lo = 0, hi = pts.length - 1;
    while (lo < hi) { var m = (lo + hi) >> 1; if (pts[m][2] < t) lo = m + 1; else hi = m; }
    return lo > 0 && t - pts[lo - 1][2] < pts[lo][2] - t ? pts[lo - 1] : pts[lo];
  }

  // o: {el, pts, steps, colour, box (the chart box), legend, play, readings, toggle, from, to (place names)}
  function drive(o) {
    var pts = o.pts || [], box = o.box, ll = pts.map(function (p) { return [p[0], p[1]]; });
    var maps = CCMap.trio(o.el, { route: ll, start: o.start, end: o.end, colour: o.colour, zoom: 16, line: pts.some(function (p) { return p[3] !== null; }) ? painter(pts, o.steps) : null });
    // the two ends named on the route, as text (a place name is what someone typed, never markup)
    var a0 = o.start || ll[0], b0 = o.end || ll[ll.length - 1];
    [[a0, o.from, "left"], [b0, o.to, "right"]].forEach(function (e) {
      if (e[0] && e[1]) L.tooltip({ permanent: true, direction: e[2], offset: [e[2] === "left" ? -8 : 8, 0], className: "tr-end", interactive: false }).setLatLng(e[0]).setContent(CCMap.lines([e[1]])).addTo(maps.route);
    });
    var timed = pts.filter(function (p) { return p[2] !== null; });
    var marks = ["route", "start", "end"].filter(function (k) { return maps[k] && timed.length; }).map(function (k) { return CCMap.follow(maps[k], timed, box, o.colour); });
    var cells = o.legend ? o.legend.querySelectorAll("[data-band]") : [];
    function at(t) {
      marks.forEach(function (m) { m.at(t); });
      var p = t === null || t === undefined || !timed.length ? null : nearestT(timed, t), b = p ? band(p[3], o.steps) : null;
      Array.prototype.forEach.call(cells, function (c) { c.classList.toggle("on", b !== null && +c.getAttribute("data-band") === b); });
    }
    if (box) box.addEventListener("cc:chart-t", function (e) { if (!e.detail) return; if (playing) pause(); at(e.detail.t); });

    // the pointer along the route moves the chart: the nearest point within a finger's width
    var hovered = false, frame = 0;
    function fromMap(e) {
      if (!timed.length) return;
      var c = e.containerPoint, best = null, bd = 28 * 28;
      timed.forEach(function (p) {
        var q = maps.route.latLngToContainerPoint([p[0], p[1]]), d = (q.x - c.x) * (q.x - c.x) + (q.y - c.y) * (q.y - c.y);
        if (d < bd) { bd = d; best = p; }
      });
      if (best) { if (playing) pause(); hovered = true; at(best[2]); if (box && box.ccChart) box.ccChart.show(best[2]); }
      else if (hovered) leave();
    }
    function leave() { hovered = false; if (playing) return; at(null); if (box && box.ccChart) box.ccChart.rest(); }
    maps.route.on("mousemove", function (e) { cancelAnimationFrame(frame); frame = requestAnimationFrame(function () { fromMap(e); }); });
    maps.route.on("click", fromMap);
    maps.route.on("mouseout", leave);

    // replay: the whole drive in 8 to 30 seconds, a minute of driving about every 1.5 s
    var playing = false, t = null, t0 = timed.length ? timed[0][2] : 0, t1 = timed.length ? timed[timed.length - 1][2] : 0, tick = 0, lastNow = 0;
    var secs = Math.min(30, Math.max(8, (t1 - t0) / 40)), btn = o.play, word = btn && btn.querySelector(".tr-w");
    var clock;
    var h12 = window.CCClock ? CCClock.hour(o.h12) : {};  // the clock Settings chose
    try { clock = new Intl.DateTimeFormat(undefined, Object.assign({ minute: "2-digit", timeZone: o.tz || undefined }, h12)); } catch (e) { clock = new Intl.DateTimeFormat(undefined, Object.assign({ minute: "2-digit" }, h12)); }
    function label() {
      if (!btn) return;
      btn.classList.toggle("on", playing);
      btn.setAttribute("aria-pressed", playing ? "true" : "false");
      btn.setAttribute("aria-label", playing ? "Pause the replay" : "Replay the drive");
      if (word) word.textContent = t !== null ? clock.format(new Date(t * 1000)) : "Replay";
    }
    function step(now) {
      if (!playing) return;
      var dt = lastNow ? (now - lastNow) / 1000 : 0;
      lastNow = now;
      t = skip(timed, Math.min(t1, t + dt * (t1 - t0) / secs), CCMap.glideGap || 120);
      at(t);
      if (box && box.ccChart) box.ccChart.show(t);  // shows without firing cc:chart-t, so the replay does not pause itself
      label();
      if (t >= t1) { playing = false; t = null; setTimeout(function () { if (!playing) { at(null); if (box && box.ccChart) box.ccChart.rest(); label(); } }, 1200); return; }
      tick = requestAnimationFrame(step);
    }
    function play() { if (t1 <= t0) return; if (t === null || t >= t1) t = t0; playing = true; lastNow = 0; label(); tick = requestAnimationFrame(step); }
    function pause() { playing = false; cancelAnimationFrame(tick); label(); }
    if (btn) {
      if (t1 <= t0) btn.hidden = true;
      btn.addEventListener("click", function () { if (playing) pause(); else play(); });
    }

    // the car's own readings over the matched line, on a switch
    var own = null;
    if (o.toggle && o.readings && o.readings.length) {
      own = L.layerGroup([L.polyline(o.readings, { color: o.colour, weight: 1.5, opacity: 0.7, dashArray: "3 5", interactive: false })].concat(
        o.readings.map(function (p) { return L.circleMarker(p, { radius: 3, color: o.colour, weight: 1.5, fillColor: "#fff", fillOpacity: 1, interactive: false }); })));
      o.toggle.addEventListener("change", function () { if (o.toggle.checked) own.addTo(maps.route); else own.remove(); });
    }
    return maps;
  }
  window.CCTrack = { drive: drive, band: band, skip: skip };
})();

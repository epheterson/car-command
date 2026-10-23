// The view in the link: what someone is looking at (the Map's centre, zoom and layers; a chart's
// switched-on lines) is kept in the page's URL query as they change it, so a copied link opens the
// same view. The address is rewritten in place (history.replaceState), never pushed, so Back still
// leaves the page. Values are short: coordinates to 4 decimals (about 10 m), zoom to 1.
(function () {
  if (window.CCView) return;  // a page that needs it at once loads it ahead of the deferred copy
  function round(v, d) { var k = Math.pow(10, d); return Math.round(v * k) / k; }
  function num(s) { return s === null || s === undefined || s === "" || !isFinite(+s) ? null : +s; }

  // centre and zoom as the two params "c" ("lat,lon") and "z"; null when there is no view to say
  function encodeMap(lat, lon, z) {
    if (!isFinite(lat) || !isFinite(lon) || !isFinite(z)) return {};
    return { c: round(lat, 4) + "," + round(lon, 4), z: String(round(z, 1)) };
  }
  // a valid centre and zoom from params, or null: out-of-range values are someone's typo, not a view
  function decodeMap(p) {
    var c = (p.c || "").split(","), lat = num(c[0]), lon = num(c[1]), z = num(p.z);
    if (c.length !== 2 || lat === null || lon === null || z === null) return null;
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180 || z < 0 || z > 22) return null;
    return { lat: lat, lon: lon, z: z };
  }

  // layers: the names switched on, against the page's defaults. Only a difference is written, so the
  // usual view has a bare link; "none" says every layer is off.
  function encodeList(on, dflt) {
    var a = on.slice().sort(), b = dflt.slice().sort();
    if (a.join() === b.join()) return null;
    return a.length ? a.join(",") : "none";
  }
  function decodeList(s, known, dflt) {
    if (s === undefined || s === null || s === "") return dflt.slice();
    if (s === "none") return [];
    return s.split(",").filter(function (k) { return known.indexOf(k) >= 0; });
  }

  // a chart's lines: {id: [keys]} against each chart's own default, as "id:k1+k2|id2:k1"
  function encodeOn(state, dflt) {
    var out = Object.keys(state).sort().filter(function (id) { return encodeList(state[id], dflt[id] || []) !== null; })
      .map(function (id) { return id + ":" + state[id].join("+"); });
    return out.length ? out.join("|") : null;
  }
  function decodeOn(s) {
    var out = {};
    (s || "").split("|").forEach(function (part) {
      var i = part.indexOf(":");
      if (i > 0) out[part.slice(0, i)] = part.slice(i + 1).split("+").filter(Boolean);
    });
    return out;
  }

  function params(search) {
    var out = {};
    new URLSearchParams(search === undefined ? location.search : search).forEach(function (v, k) { out[k] = v; });
    return out;
  }
  // the query with `changes` applied (a null or empty value removes the key)
  function merged(search, changes) {
    var q = new URLSearchParams(search);
    Object.keys(changes).forEach(function (k) { if (changes[k] === null || changes[k] === undefined || changes[k] === "") q.delete(k); else q.set(k, changes[k]); });
    var s = q.toString();
    return s ? "?" + s : "";
  }
  // Writes are gathered and made once things go quiet: a pan fires moveend again and again, and a
  // browser refuses replaceState past a few dozen calls a second (Safari throws, and the link would stop updating).
  var pending = {}, timer = 0;
  function flush() {
    timer = 0;
    var changes = pending;
    pending = {};
    try { history.replaceState(history.state, "", location.pathname + merged(location.search, changes) + location.hash); } catch (e) { /* a sandbox that forbids it: the link just stays as it was */ }
  }
  function set(changes) {
    Object.keys(changes).forEach(function (k) { pending[k] = changes[k]; });
    if (!timer) timer = setTimeout(flush, 300);
  }
  if (typeof addEventListener === "function") addEventListener("pagehide", function () { if (timer) { clearTimeout(timer); flush(); } });

  // cars hidden on the Map, named by the last six characters of the VIN: stable through renames
  // and added or removed cars, and nothing the address did not already carry
  var TAIL = /^[A-Z0-9]{6}$/;
  function tail(vin) { return String(vin).slice(-6).toUpperCase(); }
  function encodeIds(hidden) { return hidden.map(tail).filter(function (t, i, a) { return TAIL.test(t) && a.indexOf(t) === i; }).sort().join(",") || null; }
  function decodeIds(s) { return (s || "").split(",").filter(function (t, i, a) { return TAIL.test(t) && a.indexOf(t) === i; }); }
  // each chart on the page reports its switched-on lines here (keys; its own default first); a
  // change rewrites the one "ov" param for all of them
  var charts = {}, defaults = {};
  function chart(id, keys, dflt, write) {
    if (!id) return;
    charts[id] = keys.slice();
    if (!(id in defaults)) defaults[id] = dflt.slice();
    if (write) set({ ov: encodeOn(charts, defaults) });
  }
  // the lines the link asks a chart to open with (null: the chart's own)
  function chartFrom(id, known) {
    var got = decodeOn(params().ov)[id];
    return got ? got.filter(function (k, i) { return known.indexOf(k) >= 0 && got.indexOf(k) === i; }) : null;  // each line once, however the link spells it
  }
  window.CCView = { chart: chart, chartFrom: chartFrom, tail: tail, encodeIds: encodeIds, decodeIds: decodeIds, flush: flush, encodeMap: encodeMap, decodeMap: decodeMap, encodeList: encodeList, decodeList: decodeList, encodeOn: encodeOn, decodeOn: decodeOn, params: params, merged: merged, set: set };
})();

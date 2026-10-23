// The Places page (places.html): one map of every place beside a slim list, the naming card
// opened on whichever row or marker is picked, and Merge… as picking on the map.
//
// The list and the map come from one fetch (/places/index.json: the found places first, most
// stops first, then the named ones). The map's scripts are kept unrun in the page and start
// at once, in order; with map backgrounds off the vector library is never loaded.
(function () {
  "use strict";
  var wrap = document.querySelector(".plwrap");
  if (!wrap) return;
  var lists = { need: document.getElementById("pl-need"), named: document.getElementById("pl-named"), near: document.getElementById("pl-near") };
  var find = document.querySelector("[data-place-filter]"), none = document.querySelector(".plnone"), mapEl = document.getElementById("pl-map");
  var banner = document.querySelector(".plmerge");
  var all = [], byId = {}, icons = {}, sel = null, map = null, marks = {}, merging = null, q = "", showAll = false, halo = null, selPane = null, TOP = 10;
  var NAME_ZOOM = 17; // the map's zoom on a place picked to be named: street level
  var fmt = function (n) { return Number(n || 0).toLocaleString(); };

  function metres(a, b, c, d) {
    var R = 6371000, r = Math.PI / 180, x = Math.sin(((c - a) * r) / 2), y = Math.sin(((d - b) * r) / 2);
    return 2 * R * Math.asin(Math.sqrt(x * x + Math.cos(a * r) * Math.cos(c * r) * y * y));
  }
  function road(n) { var i = (n || "").lastIndexOf(", "); return i > 0 ? n.slice(0, i) : n || ""; }
  // What a found place would be called: the cars' navigation name, else its looked-up name
  // (a place on the map, or "Road, City" cut to the road); a bare coordinate stays whole.
  function suggest(p) { return p.s || (/^-?\d+(\.\d+)?, -?\d/.test(p.n) ? p.n : road(p.n)); }
  // A found place with no name from the cars or a lookup is just a coordinate: it reads as unnamed, and the coordinate is a detail beneath.
  function unnamed(p) { return !!p.a && !p.s && /^-?\d+(\.\d+)?, -?\d/.test(p.n); }
  function label(p) { return p.a ? (unnamed(p) ? "Unnamed place" : suggest(p)) : p.n; }
  function visitsText(v) { return fmt(v) + (v === 1 ? " visit" : " visits"); } // placespage.visits: one count, one word
  function title(p) { return label(p) + (p.v ? " · " + visitsText(p.v) : ""); } // no visits, no count: "0 visits" says nothing
  function svg(key) {
    var d = icons[key];
    return d ? '<svg class="pl-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + d + "</svg>" : "";
  }
  function matches(p) { return !q || (p.n + " " + (p.c || "") + " " + (p.s || "")).toLowerCase().indexOf(q) >= 0; }

  // ---- The list -------------------------------------------------------------------------------
  function row(p, far) {
    var li = document.createElement("li"), b = document.createElement("button"), ic = document.createElement("span"), t = document.createElement("span");
    var name = document.createElement("b"), sub = document.createElement("span"), go = document.createElement("a");
    li.className = "plr";
    li.dataset.id = p.id;
    b.type = "button";
    b.className = "plr-b";
    b.setAttribute("aria-pressed", "false");
    ic.className = p.a ? "pico pico-found" : "pico" + (p.ic ? "" : " pico-empty");
    ic.setAttribute("aria-hidden", "true");
    if (p.a) ic.appendChild(document.createElement("i"));
    else ic.innerHTML = svg(p.ic); // the site's own icon paths
    t.className = "ptext";
    name.textContent = label(p);
    sub.className = "muted small";
    if (unnamed(p)) li.className += " plr-unnamed";
    var bits = [];
    if (unnamed(p)) bits.push(p.n);
    if (far != null) bits.push(far < 1000 ? Math.round(far) + " m" : (far / 1000).toFixed(1) + " km");
    if (p.c) bits.push(p.c);
    if (p.v) bits.push(visitsText(p.v));
    sub.textContent = bits.join(" · ");
    t.appendChild(name);
    t.appendChild(sub);
    b.appendChild(ic);
    b.appendChild(t);
    li.appendChild(b);
    if (p.a && far == null) {
      var it = document.createElement("span");
      it.className = "plr-name";
      it.textContent = "Name it";
      b.appendChild(it);
      return li;
    }
    go.className = "plr-go";
    go.href = (document.querySelector('meta[name="cc-root"]').content || "") + "/places/" + p.id;
    go.setAttribute("aria-label", "Open " + p.n);
    go.textContent = "›";
    li.appendChild(go);
    return li;
  }
  // Found places can run to hundreds: "To name" lists them busiest first (the index's order),
  // the top ten and Show all; a search lists every one that matches. The map shows them all.
  function render() {
    lists.need.innerHTML = "";
    lists.named.innerHTML = "";
    var any = false, need = 0, named = 0, shown = 0;
    all.forEach(function (p) {
      var on = matches(p);
      if (p.a) need++; else named++;
      if (!on) return;
      any = true;
      if (!p.a) { lists.named.appendChild(row(p)); return; }
      shown++;
      if (showAll || q || shown <= TOP) lists.need.appendChild(row(p));
    });
    if (!showAll && !q && shown > TOP) {
      var li = document.createElement("li"), b = document.createElement("button");
      li.className = "plr plr-more";
      b.type = "button";
      b.className = "plr-all";
      b.textContent = "Show all " + fmt(shown);
      li.appendChild(b);
      lists.need.appendChild(li);
    }
    document.querySelector(".plsec-need .n").textContent = fmt(need);
    document.querySelector(".plsec-named .n").textContent = fmt(named);
    var head = document.querySelector("[data-pl-count]");  // the header counts what the lists below now hold
    if (head && !q && !merging) head.textContent = fmt(named) + " place" + (named === 1 ? "" : "s") + (need ? " · " + fmt(need) + " to name" : "");
    document.querySelector(".plsec-need").hidden = merging || !lists.need.children.length;
    document.querySelector(".plsec-named").hidden = !!merging;
    var empty = document.querySelector(".plempty");
    if (empty) empty.hidden = named > 0;
    none.hidden = any || !q;
    if (merging) renderNear();
    mark();
    var picked = sel && rowOf(sel.id);
    if (picked) picked.setAttribute("aria-pressed", "true");
  }
  function rowOf(id) { return wrap.querySelector('.plr[data-id="' + id + '"] .plr-b'); }

  // ---- The map --------------------------------------------------------------------------------
  function loadMap(ready) {
    if (window.L && window.CCMap) return ready();
    var t = document.getElementById("pl-maphead"), tiles = document.body.dataset.tiles === "public", last = null;
    if (!t) return;
    Array.prototype.forEach.call(t.content.children, function (n) {
      if (!tiles && /maplibre/.test(n.getAttribute("src") || n.getAttribute("href") || "")) return;
      var el = document.createElement(n.tagName.toLowerCase());
      Array.prototype.forEach.call(n.attributes, function (a) { el.setAttribute(a.name, a.value); });
      if (el.tagName === "SCRIPT") { el.async = false; last = el; }
      document.head.appendChild(el);
    });
    if (last) last.addEventListener("load", function () { if (window.L && window.CCMap) ready(); });
  }
  function draw() {
    if (map || !window.CCMap) return;
    map = CCMap.base(mapEl, { scrollWheelZoom: true, preferCanvas: true, hint: true, full: true });
    map.createPane("plsel").style.zIndex = 650; // the picked found place, above the markers
    map.getPane("plsel").style.pointerEvents = "none";
    selPane = L.svg({ pane: "plsel" });
    all.forEach(addMark);
    fit();
    if (sel) highlight(sel, true);
    if (merging) shade();
  }
  function addMark(p) {
    if (!map) return;
    var m;
    if (p.a) {
      m = L.circleMarker([p.la, p.lo], { radius: dot(p), color: CCMap.palette[3], weight: 1.5, fillOpacity: 0.45 });
    } else {
      m = L.marker([p.la, p.lo], { title: p.n, alt: p.n, keyboard: true, riseOnHover: true, icon: L.divIcon({ className: "plm" + (p.ic ? "" : " plm-none"), html: svg(p.ic), iconSize: [28, 28] }) });
    }
    m.on("click", function () { if (merging) toggle(p); else select(p, "map"); });
    m.addTo(map);
    marks[p.id] = m;
  }
  function dot(p) { return 4 + Math.min(8, Math.sqrt(p.v || 0)); }
  function dropMark(id) { if (marks[id]) { marks[id].remove(); delete marks[id]; } }
  // Shown or not by the search; a marker hidden by the search is off the map, not just faint.
  function mark() {
    if (!map) return;
    all.forEach(function (p) {
      var m = marks[p.id], on = matches(p);
      if (!m) return;
      if (on && !map.hasLayer(m)) m.addTo(map);
      else if (!on && map.hasLayer(m)) m.remove();
    });
  }
  // The view: the busiest places (a trip a thousand kilometres away would zoom everything out).
  function fit() {
    var top = all.slice().sort(function (a, b) { return b.v - a.v; }).slice(0, 40);
    if (!top.length) { map.setView([20, 0], 2); return; }
    map.fitBounds(L.latLngBounds(top.map(function (p) { return [p.la, p.lo]; })), { padding: [28, 28], maxZoom: 15 });
  }
  // The picked place stands out while its card is open: larger, ringed in the ink, above
  // every other marker (a found one is redrawn in its own pane over them); the rest dim.
  function highlight(p, on) {
    var m = marks[p.id];
    mapEl.classList.toggle("plfocus", on);
    all.forEach(function (x) {
      var c = marks[x.id];
      if (c && x.a && x.id !== p.id) c.setStyle(on ? { opacity: 0.35, fillOpacity: 0.16 } : { opacity: 1, fillOpacity: 0.45 });
    });
    if (halo) { halo.remove(); halo = null; }
    if (!m) return;
    if (p.a) {
      m.setStyle({ opacity: on ? 0 : 1, fillOpacity: on ? 0 : 0.45 });
      var ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim();
      if (on) halo = L.circleMarker([p.la, p.lo], { pane: "plsel", renderer: selPane, radius: dot(p) + 5, color: ink, weight: 3, fillColor: CCMap.palette[3], fillOpacity: 0.9, interactive: false }).addTo(map);
    } else if (m.getElement()) { m.getElement().classList.toggle("on", on); m.setZIndexOffset(on ? 1000 : 0); }
  }
  // Where the card sits beside a place on the map: a small box around its point.
  function pointRect(p) {
    return function () {
      var pt = map.latLngToContainerPoint([p.la, p.lo]), r = mapEl.getBoundingClientRect();
      return { left: r.left + pt.x - 18, right: r.left + pt.x + 18, top: r.top + pt.y - 18, bottom: r.top + pt.y + 18, width: 36, height: 36 };
    };
  }

  // ---- Picking and naming --------------------------------------------------------------------
  function select(p, from) {
    if (sel) {
      highlight(sel, false);
      var old = rowOf(sel.id);
      if (old) old.setAttribute("aria-pressed", "false");
    }
    sel = p;
    if (!p) return;
    if (p.a && !rowOf(p.id) && !showAll && !q) { showAll = true; render(); }
    var b = rowOf(p.id);
    if (b) { b.setAttribute("aria-pressed", "true"); if (from !== "list") b.scrollIntoView({ block: "nearest" }); }
    var anchor = b, wide = !(window.matchMedia && matchMedia("(max-width: 600px)").matches);
    if (map) {
      highlight(p, true);
      // Zoom to street level on the place, so it is plain where it is (never further out than that).
      map.setView([p.la, p.lo], Math.max(map.getZoom(), NAME_ZOOM), { animate: false });
      // On a wide screen the point moves to the map's left third and the card sits to its right,
      // so both stay in sight; a phone's sheet sits under the map anyway.
      var size = map.getSize(), at = map.latLngToContainerPoint([p.la, p.lo]);
      if (wide) map.panBy([at.x - size.x * 0.3, at.y - size.y * 0.45], { animate: false });
      else {
        // a phone: the map back in sight under the bar, the point in its upper part, clear of the sheet
        var bar = document.querySelector(".phonebar"), top = mapEl.getBoundingClientRect().top - (bar ? bar.offsetHeight : 0) - 8;
        if (Math.abs(top) > 4) window.scrollBy(0, top);
        map.panBy([at.x - size.x / 2, at.y - size.y * 0.3], { animate: false });
      }
      anchor = pointRect(p);
    }
    card(function (c) { c.open({
      lat: p.la,
      lon: p.lo,
      id: p.id,
      anchor: anchor,
      title: title(p),
      sticky: true,
      side: true,
      onSaved: saved,
      onClose: function () { if (sel === p) select(null); },
      onMerge: function (place) { startMerge(byId[place.id]); },
    }); });
  }
  // The card's script, loaded by shell.js on first use (and on idle once the list is here).
  function card(then) { if (window.ccPlaceCard) window.ccPlaceCard().then(then).catch(function () {}); }
  // After a save: the saved place as it is now (named, so it leaves "Needs a name"; or taken
  // into another, so it leaves the page). A place that was found goes back to the list, the map
  // back to the overview; the next one is never opened for you.
  function saved(place, was) {
    var p = sel, wasFound = p && p.a;
    if (p && place.id !== p.id) gone(p.id);
    put(place, p && place.id === p.id ? p.v : null);
    render();
    if (wasFound) { select(null); if (map) fit(); return false; }
    select(byId[place.id]);
    if (map && byId[place.id]) highlight(byId[place.id], true);
    return false;
  }
  function gone(id) {
    all = all.filter(function (x) { return x.id !== id; });
    delete byId[id];
    dropMark(id);
  }
  // The place as the save gave it back, in the named list at its rank.
  function put(place, v) {
    var p = byId[place.id] || { id: place.id, v: place.visits || 0 };
    if (v != null) p.v = v;
    p.n = place.name;
    p.la = place.latitude;
    p.lo = place.longitude;
    p.r = place.radius_m;
    p.ic = place.icon;
    if (place.icon && !icons[place.icon]) {
      var g = /<svg[^>]*>([\s\S]*?)<\/svg>/.exec(place.label || "");
      if (g) icons[place.icon] = g[1];
    }
    if (place.auto) p.a = 1; else { delete p.a; delete p.s; }
    all = all.filter(function (x) { return x.id !== p.id; });
    byId[p.id] = p;
    var at = all.findIndex(function (x) { return !x.a && (x.v < p.v || (x.v === p.v && x.id > p.id)); });
    if (p.a) at = 0;
    all.splice(at < 0 ? all.length : at, 0, p);
    dropMark(p.id);
    addMark(p);
  }

  // ---- Merge: picking on the map -------------------------------------------------------------
  function startMerge(keep) {
    if (!keep || keep.a) return;
    if (window.CCPlaceCard) window.CCPlaceCard.close(true);
    select(null);
    merging = { keep: keep, picked: [] };
    banner.hidden = false;
    say();
    render();
    if (map) shade();
  }
  function say() {
    var n = merging.picked.length, s = banner.querySelector(".plmerge-say");
    s.textContent = "";
    var b = document.createElement("b");
    b.textContent = merging.keep.n;
    s.appendChild(document.createTextNode("Merging into "));
    s.appendChild(b);
    s.appendChild(document.createTextNode(n ? ": " + n + (n === 1 ? " place picked." : " places picked.") : ": tap the places nearby that are really this one."));
    var go = banner.querySelector(".plmerge-go");
    go.disabled = !n;
    go.textContent = n ? "Merge " + n + "…" : "Merge…";
  }
  function near() {
    var k = merging.keep;
    return all.filter(function (p) { return p.id !== k.id && matches(p); }).map(function (p) { return { p: p, m: metres(k.la, k.lo, p.la, p.lo) }; })
      .filter(function (x) { return x.m <= 1000; }).sort(function (a, b) { return a.m - b.m; });
  }
  function renderNear() {
    var sec = document.querySelector(".plsec-near");
    sec.hidden = false;
    lists.near.innerHTML = "";
    var list = near();
    list.forEach(function (x) {
      var li = row(x.p, x.m);
      li.querySelector(".plr-b").setAttribute("aria-pressed", merging.picked.indexOf(x.p.id) >= 0 ? "true" : "false");
      lists.near.appendChild(li);
    });
    if (!list.length) lists.near.insertAdjacentHTML("beforeend", '<li class="plr-none muted small">Nothing else within a kilometre. Tap any place on the map.</li>');
  }
  function shade() {
    var k = merging && merging.keep;
    all.forEach(function (p) {
      var m = marks[p.id];
      if (!m) return;
      var close = k && p.id !== k.id && metres(k.la, k.lo, p.la, p.lo) <= 1000, picked = k && merging.picked.indexOf(p.id) >= 0;
      if (p.a) m.setStyle({ opacity: !k || close || picked ? 1 : 0.3, fillOpacity: !k || close || picked ? 0.45 : 0.12, color: picked ? CCMap.palette[1] : CCMap.palette[3], weight: picked ? 3 : 1.5 });
      else if (m.getElement()) {
        var el = m.getElement();
        el.classList.toggle("dim", !!k && !close && !picked && p.id !== k.id);
        el.classList.toggle("picked", !!picked);
        el.classList.toggle("keep", !!k && p.id === k.id);
      }
    });
    if (ring) { ring.remove(); ring = null; }
    if (k) {
      ring = L.circle([k.la, k.lo], { radius: 1000, color: CCMap.palette[1], weight: 1.5, dashArray: "5 6", fill: false, interactive: false }).addTo(map);
      map.fitBounds(L.latLng(k.la, k.lo).toBounds(2000), { maxZoom: 16 });
    }
  }
  var ring = null;
  function toggle(p) {
    if (!merging || p.id === merging.keep.id) return;
    var i = merging.picked.indexOf(p.id);
    if (i >= 0) merging.picked.splice(i, 1); else merging.picked.push(p.id);
    say();
    var b = lists.near.querySelector('.plr[data-id="' + p.id + '"] .plr-b');
    if (b) b.setAttribute("aria-pressed", i >= 0 ? "false" : "true");
    if (map) { var c = map.getCenter(), z = map.getZoom(); shade(); map.setView(c, z, { animate: false }); }
  }
  function endMerge() {
    merging = null;
    banner.hidden = true;
    document.querySelector(".plsec-near").hidden = true;
    render();
    if (map) shade();
  }
  banner.querySelector(".plmerge-cancel").addEventListener("click", endMerge);
  banner.querySelector(".plmerge-go").addEventListener("click", function (e) {
    if (!merging || !merging.picked.length) return;
    var keep = merging.keep, at = e.currentTarget;
    card(function (c) { c.confirmMerge({
      keep: keep.id,
      others: merging.picked.slice(),
      anchor: at,
      onDone: function (place, merged) {
        var v = keep.v || 0;
        merged.forEach(function (id) { v += (byId[id] && byId[id].v) || 0; gone(id); });
        endMerge();
        put(place, v);
        render();
        return false;
      },
    }); });
  });

  // ---- Wiring ---------------------------------------------------------------------------------
  wrap.addEventListener("click", function (e) {
    if (e.target.closest(".plr-all")) { showAll = true; render(); return; }
    var b = e.target.closest(".plr-b");
    if (!b) return;
    var p = byId[+b.closest(".plr").dataset.id];
    if (!p) return;
    if (merging) toggle(p); else select(p, "list");
  });
  find.addEventListener("input", function () {
    q = find.value.trim().toLowerCase();
    render();
  });

  fetch(wrap.dataset.index, { credentials: "same-origin" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (d) {
      all = d.places || [];
      icons = d.icons || {};
      all.forEach(function (p) { byId[p.id] = p; });
      render();
      loaded = true;
      maybe();
      if (window.requestIdleCallback) requestIdleCallback(function () { card(function () {}); });
      var m = +wrap.dataset.merge;
      if (m && byId[m]) startMerge(byId[m]);
    })
    .catch(function () {
      lists.need.innerHTML = lists.named.innerHTML = "";
      none.hidden = false;
      none.textContent = "The places could not be loaded. Reload the page to try again.";
    });
  // the map draws once both its scripts and the places are here, whichever comes second
  var loaded = false, libs = false;
  function maybe() { if (loaded && libs) draw(); }
  loadMap(function () { libs = true; maybe(); });

})();

// The naming card, and the icon row it shares with a place's own page.
//
// Any place name on any page (a `.pn`, filters.place_refs) opens the card where it was tapped:
// a popover beside it on a wide screen, a sheet from the bottom on a phone. shell.js loads this
// file on the first tap; the Places page and a place's page load it with the page. The card's
// markup comes once from /places/card, its suggestions from /places/candidates; Save posts to
// the routes a place's page posts to (/places/{id}/edit for a place, /places/pin for a stop no
// place covers), asking for JSON, and every name on the page inside the saved place changes
// in place. Nothing is moved or resized from here: the card never sends "moved" or "resized".
(function () {
  "use strict";
  function meta(n) { var m = document.querySelector('meta[name="' + n + '"]'); return (m && m.content) || ""; }
  var ROOT = meta("cc-root");
  var fine = !!(window.matchMedia && matchMedia("(hover: hover) and (pointer: fine)").matches);
  function sheet() { return !!(window.matchMedia && matchMedia("(max-width: 600px)").matches); }

  // ---- The icon row (_icons.html's icon_row) ------------------------------------------------
  // The row carries the likely icons; More… opens every icon, fetched once from the cached
  // /icons/places.json (key -> [title, paths, search words]), with a search over them.
  var asked = null;
  function iconSet(v) {
    if (!asked) {
      asked = fetch(ROOT + "/icons/places.json?v=" + encodeURIComponent(v || ""), { credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .catch(function (err) { asked = null; throw err; });
    }
    return asked;
  }
  // the same markup as placeicons.svg and the place_icon macro. Built as elements: the key
  // (read back from the page) only ever lands in an attribute, never in parsed markup; the
  // paths are the site's own icon set from /icons/places.json.
  var SVG = "http://www.w3.org/2000/svg";
  var LOOK = { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" };
  function glyph(key, paths) {
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "pl-ic pl-" + key);
    Object.keys(LOOK).forEach(function (a) { svg.setAttribute(a, LOOK[a]); });
    var parsed = new DOMParser().parseFromString('<svg xmlns="' + SVG + '">' + paths + "</svg>", "image/svg+xml").documentElement;
    Array.prototype.slice.call(parsed.childNodes).forEach(function (n) { svg.appendChild(document.importNode(n, true)); });
    return svg;
  }
  function option(key, row) {
    var label = document.createElement("label"), input = document.createElement("input");
    label.className = "icopt";
    label.title = row[0];
    input.type = "radio";
    input.name = "icon";
    input.value = key;
    input.setAttribute("aria-label", "Icon: " + row[0]);
    label.appendChild(input);
    label.appendChild(glyph(key, row[1]));
    return label;
  }
  function fill(box, data) {
    if (box.dataset.full) return;
    box.dataset.full = "1";
    var opts = box.querySelector(".icofull .icoopts");
    Object.keys(data).forEach(function (k) {
      var l = option(k, data[k]);
      l.dataset.words = (k.replace(/-/g, " ") + " " + data[k][0] + " " + data[k][2]).toLowerCase();
      opts.appendChild(l);
    });
  }
  function filter(box, q) {
    var words = q.toLowerCase().split(/\s+/).filter(Boolean), any = false;
    box.querySelectorAll(".icofull .icopt").forEach(function (l) {
      var on = words.every(function (x) { return (l.dataset.words || "").indexOf(x) >= 0; });
      l.hidden = !on;
      any = any || on;
    });
    box.querySelector(".icoempty").hidden = any || !words.length;
  }
  function openFull(box, on) {
    var full = box.querySelector(".icofull"), more = box.querySelector(".icomore"), search = full.querySelector(".icosearch");
    full.hidden = !on;
    more.setAttribute("aria-expanded", on ? "true" : "false");
    more.textContent = on ? "Fewer" : "More…";
    if (!on) return;
    iconSet(box.dataset.iconsV).then(function (data) { fill(box, data); filter(box, search.value); }).catch(function () {
      box.querySelector(".icoempty").hidden = false;
      box.querySelector(".icoempty").textContent = "The icons could not be loaded.";
    });
    if (fine) search.focus();
  }
  // The row's own radio for `key`, put in front of the row when the row lacks it.
  function quick(box, key) {
    var row = box.querySelector(".icoquick"), sel = 'input[value="' + CSS.escape(key) + '"]';
    var have = row.querySelector(sel);
    if (have) return Promise.resolve(have);
    return iconSet(box.dataset.iconsV).then(function (data) {
      if (!data[key]) return null;
      var again = row.querySelector(sel);
      if (again) return again;
      var l = option(key, data[key]);
      row.insertBefore(l, row.firstChild);
      return l.querySelector("input");
    });
  }
  // Check `key` in a row (a guess, or the place's own); null checks nothing, so nothing posts.
  // A later call wins over an earlier one still fetching.
  var latest = 0;
  function setIcon(box, key) {
    if (!box) return;
    var mine = ++latest;
    box.querySelectorAll(".icopt.eff").forEach(function (l) { l.classList.remove("eff"); });
    if (!key) { box.querySelectorAll('input[name="icon"]').forEach(function (r) { r.checked = false; }); return; }
    quick(box, key).then(function (input) { if (mine === latest && input) input.checked = true; }).catch(function () {});
  }

  // placeicons.icon_for_name, the same rule: the first phrase of the list (in its order, so
  // [phrase, icon] pairs, not an object) that is in the name as whole words ("SF Home",
  // "Mom's home", "home 2"), any case; " * " is any words between ("home * road"); a
  // phrase whose icon is null ("nursing home", "home * road") stops the guess. "Workshop"
  // is not "work".
  var patterns = {};
  function pattern(phrase) {
    if (!patterns[phrase]) {
      var parts = phrase.split(" * ").map(function (p) { return "(?:^|[^\\p{L}\\p{N}])" + p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?![\\p{L}\\p{N}])"; });
      patterns[phrase] = new RegExp(parts.join(".*"), "u");
    }
    return patterns[phrase];
  }
  function iconForName(name, pairs) {
    var n = (name || "").toLowerCase().replace(/\s+/g, " ").trim();
    if (!n) return null;
    for (var i = 0; i < pairs.length; i++) if (pattern(pairs[i][0]).test(n)) return pairs[i][1];
    return null;
  }
  function pairsOf(box) {
    if (!box) return [];
    if (!box._pairs) { try { box._pairs = JSON.parse(box.dataset.nameIcons || "[]"); } catch (_) { box._pairs = []; } }
    return box._pairs;
  }
  // Mark `key` as the icon shown without checking it: nothing posts, the stored icon stays.
  function mark(box, key) {
    if (!box || !key) return;
    quick(box, key).then(function (input) { if (input && !box.querySelector('input[name="icon"]:checked')) input.parentElement.classList.add("eff"); }).catch(function () {});
  }
  // A guess (a typed name, a suggestion), unless this form's icon was chosen by hand. The house
  // is never ticked by a guess: it makes a place home and re-prices its charges, so a guessed
  // house is only marked, and home is a tap on it.
  function guess(form, key) {
    if (!form || form.dataset.iconHand) return;
    var box = form.querySelector(".icorow");
    if (key === HOME) { setIcon(box, null); mark(box, key); } else setIcon(box, key);
  }
  var HOME = "home";

  document.addEventListener("click", function (e) {
    var more = e.target.closest && e.target.closest(".icomore");
    if (!more) return;
    var box = more.closest(".icorow");
    openFull(box, box.querySelector(".icofull").hidden);
  });
  document.addEventListener("change", function (e) {
    var input = e.target;
    if (!input.matches || !input.matches('.icorow input[name="icon"]')) return;
    var box = input.closest(".icorow");
    // a real tap marks the icon as the driver's; a guess (no event at all) never does
    if (e.isTrusted && input.form) input.form.dataset.iconHand = "1";
    box.querySelectorAll(".icopt.eff").forEach(function (l) { l.classList.remove("eff"); });
    if (input.closest(".icofull")) quick(box, input.value).then(function (q) { if (q) q.checked = true; openFull(box, false); });
  });
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (!t.matches) return;
    if (t.matches(".icosearch")) {
      var box = t.closest(".icorow");
      filter(box, t.value);
    } else if (t.matches("input[data-auto-icon]") && t.form) {
      t.dataset.typed = "1";
      guess(t.form, iconForName(t.value, pairsOf(t.form.querySelector(".icorow"))));
    }
    var addr = t.matches('input[name="address"]') && t.closest(".addr-edit");
    if (addr) showReset(addr);
  });
  // In the search box: Enter takes the first icon shown (never the form's submit), Escape
  // closes the box back onto the row.
  document.addEventListener("keydown", function (e) {
    var box = e.target.matches && e.target.matches(".icosearch") && e.target.closest(".icorow");
    if (!box) return;
    if (e.key === "Enter") {
      e.preventDefault();
      var first = box.querySelector(".icofull .icopt:not([hidden]) input");
      if (first) first.click();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      openFull(box, false);
      box.querySelector(".icomore").focus();
    }
  });

  // "Use the looked-up address" beside an address the owner typed over (a place's page).
  function showReset(box) {
    var input = box.querySelector('input[name="address"]'), lookup = box.querySelector('input[name="address_lookup"]'), btn = box.querySelector(".pa-reset");
    if (!input || !btn) return;
    var was = lookup ? lookup.value.trim() : "";
    btn.dataset.resetTo = was;
    btn.hidden = !was || input.value.trim() === was || !input.value.trim();
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".pa-reset");
    if (!btn) return;
    var input = btn.closest(".addr-edit").querySelector('input[name="address"]');
    input.value = btn.dataset.resetTo || "";
    btn.hidden = true;
    input.focus();
  });

  // ---- The card -----------------------------------------------------------------------------
  var card = null, form, nameIn, chips, icons, price, dup, err, at, head, details, mergeBtn, keys, confirm, tip, built = null;
  var kinds = {}, skeleton = "", st = null, seq = 0, priceTouched = false;

  function build() {
    if (built) return built;
    built = fetch(ROOT + "/places/card", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        var t = document.createElement("template");
        t.innerHTML = html.trim();
        card = t.content.firstElementChild;
        document.body.appendChild(card);
        // the pointer from a card beside a point on a map to the point; outside the card,
        // which scrolls and would clip it
        tip = document.createElement("div");
        tip.className = "pcard-tip";
        tip.hidden = true;
        document.body.appendChild(tip);
        form = card.querySelector(".pcard-f");
        nameIn = form.querySelector('input[name="name"]');
        chips = form.querySelector(".pcard-chips");
        skeleton = chips.innerHTML;
        icons = form.querySelector(".icorow");
        price = form.querySelector(".pprice");
        dup = form.querySelector(".pcard-dup");
        err = form.querySelector(".pcard-err");
        at = form.querySelector(".pcard-at");
        head = form.querySelector("#pcard-h");
        details = form.querySelector(".pcard-details");
        mergeBtn = form.querySelector(".pcard-merge");
        keys = form.querySelector(".pcard-keys");
        confirm = card.querySelector(".pcard-confirm");
        try { kinds = JSON.parse(card.dataset.kindIcons || "{}"); } catch (_) { kinds = {}; }
        wire();
        return card;
      })
      .catch(function (e) { built = null; throw e; });
    return built;
  }

  function wire() {
    form.addEventListener("submit", function (e) { e.preventDefault(); save({}); });
    card.querySelectorAll(".pcard-x").forEach(function (b) { b.addEventListener("click", function () { close(); }); });
    price.addEventListener("change", function () { priceTouched = true; });
    price.addEventListener("input", function () { priceTouched = true; });
    chips.addEventListener("click", function (e) {
      var c = e.target.closest("button.pchipb");
      if (!c) return;
      chips.querySelectorAll("button.pchipb").forEach(function (b) { b.setAttribute("aria-pressed", b === c ? "true" : "false"); });
      nameIn.value = c.dataset.name;
      nameIn.dataset.typed = "1";
      guess(form, kinds[c.dataset.kind] || iconForName(c.dataset.name, pairsOf(icons)));
      if (fine) nameIn.focus();
    });
    dup.querySelector(".pcard-dupkeep").addEventListener("click", function () { save({ separate: "1" }); });
    dup.querySelector(".pcard-dupmerge").addEventListener("click", mergeOffered);
    mergeBtn.addEventListener("click", function () {
      var p = st && st.place, o = st && st.o;
      if (!p) return;
      if (o && o.onMerge) { close(true); o.onMerge(p); } else location.href = ROOT + "/places?merge=" + p.id;
    });
    confirm.querySelector(".pcard-back").addEventListener("click", function () {
      var o = st && st.o;
      if (o && o.onBack) { close(true); o.onBack(); } else close();
    });
    confirm.querySelector(".pcard-domerge").addEventListener("click", doMerge);
    card.addEventListener("keydown", function (e) {
      // Escape in the icon search closes the search (the row's own handler), not the card
      if (e.key !== "Escape" || e.defaultPrevented || e.target.matches(".icosearch")) return;
      e.preventDefault();
      var o = st && st.o;
      if (o && o.queue && o.onSkip && confirm.hidden) { close(true); o.onSkip(); } else close();
    });
    // a tap anywhere else closes it, unless it lands on another place name (that one opens)
    // or the page keeps the card open while its map is used (the Places page)
    document.addEventListener("pointerdown", function (e) {
      if (!st || card.hidden || card.contains(e.target) || (e.target.closest && e.target.closest(".pn")) || (st.o && st.o.sticky)) return;
      close();
    }, true);
    var queued = false;
    function again() { if (queued) return; queued = true; requestAnimationFrame(function () { queued = false; position(); }); }
    addEventListener("resize", again);
    addEventListener("scroll", again, true);
  }

  function rectOf(a) {
    if (!a) return null;
    if (typeof a === "function") return a();
    return a.getBoundingClientRect ? a.getBoundingClientRect() : null;
  }
  // Beside what was tapped, inside the window: below it, or above when there is more room
  // there; a point on a map (`side`) has it to its right or left, so the point stays in sight.
  function position() {
    if (tip) tip.hidden = true;
    if (!card || card.hidden || !st) return;
    if (sheet()) { card.style.left = ""; card.style.top = ""; return; }
    var r = rectOf(st.anchor), w = card.offsetWidth, h = card.offsetHeight, vw = document.documentElement.clientWidth, vh = innerHeight, m = 8, left, top;
    var clampX = function (x) { return Math.min(Math.max(m, x), vw - w - m); }, clampY = function (y) { return Math.max(m, Math.min(y, vh - h - m)); };
    if (!r) { left = (vw - w) / 2; top = Math.max(m, (vh - h) / 3); }
    else if (st.o.side && (vw - r.right - m >= w + 12 || r.left - m >= w + 12)) {
      left = vw - r.right - m >= w + 12 ? r.right + 12 : r.left - 12 - w;
      top = clampY(r.top + r.height / 2 - h / 2);
      var mid = r.top + r.height / 2;
      if (mid > top + 16 && mid < top + h - 16) {
        tip.hidden = false;
        tip.classList.toggle("pcard-tip-r", left < r.left); // the card left of the point
        tip.style.left = Math.round(left < r.left ? left + w - 7 : left - 7) + "px";
        tip.style.top = Math.round(mid - 7) + "px";
      }
    } else {
      left = clampX(r.left);
      var below = vh - r.bottom - m, above = r.top - m;
      top = clampY(below >= h || below >= above ? r.bottom + 6 : r.top - 6 - h);
    }
    card.style.left = Math.round(left) + "px";
    card.style.top = Math.round(top) + "px";
  }

  function view(which) {
    form.hidden = which !== "form";
    confirm.hidden = which !== "confirm";
  }
  function say(box, text) { box.textContent = text || ""; box.hidden = !text; }

  function setPrice(p, home) {
    var none = price.querySelector('input[name="price_kind"][value=""]'), homeOpt = price.querySelector('input[name="price_kind"][value="home"]');
    none.nextElementSibling.textContent = home ? "Home plan" : "Not set";
    if (homeOpt) homeOpt.closest("label").hidden = !!home;
    price.querySelectorAll(".pp-paid input").forEach(function (i) { i.value = ""; });
    price.querySelector('select[name="price_per"]').value = "kwh";
    var kind = p && p.kind, pick = none;
    if (kind === "kwh" || kind === "minute") {
      pick = price.querySelector('input[name="price_kind"][value="paid"]');
      price.querySelector('select[name="price_per"]').value = kind;
      price.querySelector('input[name="price_rate"]').value = p.rate;
      if (p.fee) price.querySelector('input[name="price_fee"]').value = p.fee;
    } else if (kind) {
      pick = price.querySelector('input[name="price_kind"][value="' + CSS.escape(kind) + '"]');
    }
    // a price the card has no chip for (Average) shows none checked, and is sent only if changed
    price.querySelectorAll('input[name="price_kind"]').forEach(function (r) { r.checked = r === pick; });
    priceTouched = false;
  }

  function reset() {
    view("form");
    nameIn.value = "";
    delete nameIn.dataset.typed;
    chips.innerHTML = skeleton;
    chips.setAttribute("aria-busy", "true");
    delete form.dataset.iconHand;
    setIcon(icons, null);
    openFull(icons, false);
    setPrice(null, false);
    say(err, "");
    dup.hidden = true;
    at.textContent = "";
    head.textContent = st.o.title || "Name this place";
    head.classList.toggle("pcard-title", !!st.o.title);
    details.href = ROOT + "/places/new?lat=" + encodeURIComponent(st.lat) + "&lon=" + encodeURIComponent(st.lon);
    mergeBtn.hidden = true;
    keys.hidden = !(st.o.queue && fine);
    form.querySelector('input[name="address_lookup"]').value = "";
    form.querySelector(".pcard-save").disabled = false;
  }

  // Open the card on a spot: { lat, lon } of a stop or of a place; `id` when it is that place
  // (the Places page); `anchor`, what it sits beside (an element, or a function giving a rect);
  // `onSaved(place, was)`, `onSkip()`, `onClose()`, `onMerge(place)`: the page's own steps,
  // `queue`: Esc skips to the next instead of closing; `sticky`: taps elsewhere leave it open;
  // `title`: the header, naming what was picked ("Elm Street · 31 visits").
  function open(o) {
    return build().then(function () {
      st = { o: o || {}, lat: +o.lat, lon: +o.lon, id: o.id || null, anchor: o.anchor || null, place: null, seq: ++seq };
      reset();
      card.hidden = false;
      position();
      if (fine) nameIn.focus({ preventScroll: true });
      else card.focus({ preventScroll: true });
      lookup();
    });
  }

  function addressLine(a) {
    if (!a) return "";
    if (typeof a === "string") return a;
    var street = [a.house_number || a.housenumber, a.street || a.road].filter(Boolean).join(" ");
    return [street, a.city || a.town].filter(Boolean).join(", ") || a.display_name || a.label || "";
  }

  function lookup() {
    var mine = st.seq;
    var url = ROOT + "/places/candidates?lat=" + encodeURIComponent(st.lat) + "&lon=" + encodeURIComponent(st.lon) + (st.id ? "&place=" + encodeURIComponent(st.id) : "");
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (st && mine === st.seq) show(d || {}); })
      .catch(function () { if (st && mine === st.seq) show({}); });
  }

  // The suggestions, best first: what the cars navigated to, then the map's places nearby.
  function suggestions(d) {
    var out = [], seen = {};
    function add(name, kind, sub) {
      var k = (name || "").trim().toLowerCase();
      if (!k || seen[k]) return;
      seen[k] = 1;
      out.push({ name: name.trim(), kind: kind || "", sub: sub || "" });
    }
    if (d.nav) {
      add(d.nav.name, "", d.nav.source);
      (d.nav.alternates || []).forEach(function (n) { add(n, "", d.nav.source); });
    }
    (d.candidates || []).forEach(function (c) { add(c.name, c.kind, c.inside ? "you were here" : Math.round(c.distance_m || 0) + " m"); });
    return out;
  }

  function show(d) {
    var p = d.place || null, named = !!(p && !p.auto), lookupText = addressLine(d.address) || d.place_address || "";
    st.place = p;
    form.querySelector('input[name="address_lookup"]').value = lookupText;
    head.textContent = st.o.title || (named ? "Edit place" : "Name this place");
    var where = named ? d.user_address || lookupText : lookupText;
    at.textContent = [where, p && p.visits && !st.o.title ? p.visits + (p.visits === 1 ? " visit" : " visits") : ""].filter(Boolean).join(" · ");
    nameIn.placeholder = lookupText ? "Name, or blank for " + lookupText : "Name";
    var sugg = suggestions(d);
    var best = named ? p.name : sugg.length ? sugg[0].name : p ? p.name : "";
    if (!nameIn.dataset.typed) {
      nameIn.value = best;
      if (fine && document.activeElement === nameIn) nameIn.select();
    }
    chips.innerHTML = "";
    chips.setAttribute("aria-busy", "false");
    sugg.slice(0, 7).forEach(function (s) {
      var b = document.createElement("button"), em = document.createElement("small");
      b.type = "button";
      b.className = "pchipb";
      b.dataset.name = s.name;
      b.dataset.kind = s.kind;
      b.setAttribute("aria-pressed", s.name === nameIn.value ? "true" : "false");
      b.appendChild(document.createTextNode(s.name));
      if (s.sub) { em.textContent = s.sub; b.appendChild(em); }
      chips.appendChild(b);
    });
    if (!sugg.length) chips.insertAdjacentHTML("beforeend", '<span class="muted small pcard-none">' + (d.available === false && !d.nav ? "No suggestions: the local maps add-on isn’t running." : "Nothing named nearby.") + "</span>");
    if (named && p.stored_icon) setIcon(icons, p.stored_icon);
    else if (named) mark(icons, p.icon); // the name's guess: marked, not checked, so a Save keeps none stored
    else guess(form, (sugg[0] && kinds[sugg[0].kind]) || iconForName(nameIn.value, pairsOf(icons)));
    if (p) setPrice(p.price, p.home);
    details.href = p ? ROOT + "/places/" + p.id : details.href;
    mergeBtn.hidden = !named;
    position();
  }

  function post(url, fd) {
    fd.set("csrf_token", meta("csrf-token"));
    return fetch(ROOT + url, { method: "POST", body: fd, credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; });
    });
  }

  function save(extra) {
    if (!st || !card || card.hidden) return;
    var p = st.place, fd = new FormData(form), name = (fd.get("name") || "").trim();
    if (!priceTouched) ["price_kind", "price_rate", "price_per", "price_fee"].forEach(function (k) { fd.delete(k); });
    var rate = price.querySelector('input[name="price_rate"]');
    if (priceTouched && price.querySelector('input[value="paid"]:checked') && !(parseFloat(rate.value) > 0)) { rate.focus(); say(err, "Type what a kWh or a minute costs here."); return; }
    var url;
    if (p) {
      if (!name) name = p.auto ? p.name : "";
      if (!name) { nameIn.focus(); say(err, "A name, please."); return; }
      fd.set("name", name);
      fd.delete("address_lookup");
      url = "/places/" + p.id + "/edit";
    } else {
      if (!name && !fd.get("address_lookup")) { nameIn.focus(); say(err, "A name, please: there is no address to use."); return; }
      fd.set("lat", st.lat);
      fd.set("lon", st.lon);
      url = "/places/pin";
    }
    // A name that says home ("Home", "SF Home") with no icon stored makes the place home by its
    // name (places.is_home). Home is a tap on the house: without one, a place not home already
    // is saved with no icon, so a guess never makes it home or re-prices its charges.
    if (!fd.get("icon") && iconForName(fd.get("name"), pairsOf(icons)) === HOME && !(p && p.home)) fd.set("icon", "none");
    Object.keys(extra || {}).forEach(function (k) { fd.set(k, extra[k]); });
    send(url, fd);
  }

  function send(url, fd) {
    var btn = form.querySelector(".pcard-save"), mine = st.seq;
    btn.disabled = true;
    say(err, "");
    post(url, fd).then(function (res) {
      if (!st || mine !== st.seq) return;
      btn.disabled = false;
      if (res.status === 409 && res.j.offer) return offer(res.j.offer);
      if (res.status === 200 && res.j.place) return done(res.j.place);
      say(err, "Not saved (" + res.status + "). Try again.");
    }).catch(function () {
      if (!st || mine !== st.seq) return;
      btn.disabled = false;
      say(err, "Not saved: the site did not answer. Try again.");
    });
  }

  function far(m) { return m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(1) + " km"; }
  // The name is already a place's nearby: offer to take this one into it instead.
  function offer(o) {
    st.offer = o;
    dup.querySelector(".pcard-dupsay").textContent = o.name + " is already a place, " + far(o.m) + " away.";
    dup.querySelector(".pcard-dupmerge").textContent = "Merge into " + o.name;
    dup.hidden = false;
    dup.querySelector(".pcard-dupmerge").focus();
    position();
  }
  function mergeOffered() {
    var o = st && st.offer, p = st && st.place, fd = new FormData();
    if (!o) return;
    if (o.confirm && p) {
      // a place somebody named: through Merge's confirm sheet, never in one tap; Back returns to
      // this card as it was, the typed name and all
      var was = st;
      confirmMerge({
        keep: o.id,
        others: [p.id],
        anchor: st.anchor,
        onBack: function () { st = was; view("form"); card.hidden = false; position(); nameIn.focus({ preventScroll: true }); },
        onDone: function (place) { return !!(was.o.onSaved && was.o.onSaved(place, was.place) === true); },
      });
      return;
    }
    if (p) { fd.set("other", p.id); send("/places/" + o.id + "/merge", fd); }
    else { fd.set("lat", st.lat); fd.set("lon", st.lon); fd.set("merge_into", o.id); send("/places/pin", fd); }
  }

  function metres(a, b, c, d) {
    var R = 6371000, r = Math.PI / 180, x = Math.sin(((c - a) * r) / 2), y = Math.sin(((d - b) * r) / 2);
    return 2 * R * Math.asin(Math.sqrt(x * x + Math.cos(a * r) * Math.cos(c * r) * y * y));
  }
  // Every place name on the page inside the saved place now reads as it, icon and all.
  function apply(place, anchor) {
    var n = 0;
    document.querySelectorAll(".pn[data-lat]").forEach(function (el) {
      var la = +el.dataset.lat, lo = +el.dataset.lon;
      var hit = el === anchor || (place.circles || []).some(function (c) { return metres(la, lo, c[0], c[1]) <= c[2]; });
      if (!hit) return;
      el.innerHTML = place.label;
      el.classList.remove("pn-new");
      n++;
    });
    return n;
  }

  function done(place) {
    var o = st.o, was = st.place, anchor = st.anchor;
    apply(place, anchor);
    document.dispatchEvent(new CustomEvent("cc:place-saved", { detail: { place: place, was: was } }));
    if (o.onSaved && o.onSaved(place, was) === true) return; // the page opened the next one
    close();
  }

  // close(quiet): without handing the focus back or telling the page (it is moving on itself)
  function close(quiet) {
    if (!card || card.hidden) { st = null; return; }
    card.hidden = true;
    tip.hidden = true;
    openFull(icons, false);
    var a = st && st.anchor, o = st && st.o;
    st = null;
    if (quiet) return;
    if (a && a.focus && document.contains(a)) a.focus({ preventScroll: true });
    if (o && o.onClose) o.onClose();
  }

  // ---- Merging: the confirm view of the same card --------------------------------------------
  // { keep, others: [ids], anchor, onDone(place), onBack() }: what moves (visits, charges,
  // prices) and the name, icon and price they all take; Merge posts them in one go.
  function confirmMerge(o) {
    return build().then(function () {
      st = { o: o, anchor: o.anchor || null, seq: ++seq, merge: o };
      view("confirm");
      card.hidden = false;
      var keepP = confirm.querySelector(".pcard-keep"), list = confirm.querySelector(".pcard-mlist"), go = confirm.querySelector(".pcard-domerge"), cerr = confirm.querySelector(".pcard-err");
      confirm.querySelector(".pcard-ch").textContent = "Merge";
      keepP.textContent = "Looking at what moves…";
      list.innerHTML = "";
      say(cerr, "");
      go.disabled = true;
      position();
      go.focus({ preventScroll: true });
      var mine = st.seq;
      fetch(ROOT + "/places/" + o.keep + "/merge-preview?" + o.others.map(function (id) { return "other=" + encodeURIComponent(id); }).join("&"), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!st || mine !== st.seq) return;
          if (!d || !d.keep) { keepP.textContent = "That place is gone."; return; }
          confirm.querySelector(".pcard-ch").textContent = "Merge into " + d.keep.name;
          keepP.innerHTML = "";
          var b = document.createElement("b");
          b.innerHTML = d.keep.label; // the server's own markup: the name escaped, its icon
          keepP.appendChild(b);
          keepP.appendChild(document.createTextNode(" keeps its name, icon and price (" + d.keep.price_text + ")."));
          d.others.forEach(function (x) {
            var li = document.createElement("li"), nm = document.createElement("b"), m = document.createElement("span");
            nm.textContent = x.name;
            m.className = "muted";
            var priced = x.price_text === "Not set" ? "no price set" : "price " + x.price_text;
            m.textContent = [x.visits + (x.visits === 1 ? " visit" : " visits"), x.charges + (x.charges === 1 ? " charge" : " charges"), priced + (x.price_text === d.keep.price_text ? "" : " → " + d.keep.price_text)].join(" · ");
            li.appendChild(nm);
            li.appendChild(m);
            list.appendChild(li);
          });
          var n = d.others.length;
          go.textContent = "Merge " + n + (n === 1 ? " place" : " places");
          go.disabled = !n;
          st.others = d.others.map(function (x) { return x.id; });
          position();
        })
        .catch(function () { if (st && mine === st.seq) keepP.textContent = "Could not look: the site did not answer."; });
    });
  }
  function doMerge() {
    var o = st && st.merge;
    if (!o || !st.others) return;
    var fd = new FormData(), go = confirm.querySelector(".pcard-domerge"), cerr = confirm.querySelector(".pcard-err"), mine = st.seq;
    st.others.forEach(function (id) { fd.append("other", id); });
    go.disabled = true;
    post("/places/" + o.keep + "/merge", fd).then(function (res) {
      if (!st || mine !== st.seq) return;
      if (res.status !== 200 || !res.j.place) { go.disabled = false; say(cerr, "Not merged (" + res.status + "). Try again."); return; }
      apply(res.j.place, null);
      document.dispatchEvent(new CustomEvent("cc:place-saved", { detail: { place: res.j.place, merged: st.others } }));
      var merged = st.others;
      if (o.onDone && o.onDone(res.j.place, merged) === true) return;
      close();
    }).catch(function () { if (st && mine === st.seq) { go.disabled = false; say(cerr, "Not merged: the site did not answer."); } });
  }

  window.CCPlaceCard = { open: open, close: close, confirmMerge: confirmMerge };
})();

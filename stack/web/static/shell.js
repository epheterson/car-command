// The shell's two sheets (the car picker and More) are <details>, so they open without a
// script. This makes them behave like sheets: one open at a time, and Escape or a tap
// anywhere outside closes them.
(function () {
  var sheets = function () { return document.querySelectorAll("details.carsheet, details.more"); };
  function closeAll(except) {
    sheets().forEach(function (d) { if (d !== except) d.open = false; });
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (d && d.matches && d.matches("details.carsheet, details.more") && d.open) closeAll(d);
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var open = document.querySelector("details.carsheet[open], details.more[open]");
    if (!open) return;
    open.open = false;
    var s = open.querySelector("summary");
    if (s) s.focus();
  });
  // a tap closes every sheet but the one it lands in, on the tap itself; the toggle event
  // comes a task later, which left two sheets open for a moment
  document.addEventListener("pointerdown", function (e) {
    closeAll(e.target.closest("details.carsheet, details.more"));
  }, { passive: true });
  // a date picker that navigates as soon as a day is chosen (the Day page's bar)
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("input[data-autosubmit]") && e.target.form) e.target.form.submit();
  });
  // A form that waits on something slow (TeslaFi, a TeslaMate database) shows the mark pulling in,
  // with what it is doing, until the next page answers. The button is held so it is not sent twice.
  document.addEventListener("submit", function (e) {
    var form = e.target, say = form.getAttribute("data-wait"), tpl = document.getElementById("cc-wait");
    if (!say || !tpl || e.defaultPrevented) return;
    var at = e.submitter || form.querySelector("button[type=submit]") || form;
    var w = document.createElement("span");
    w.className = "wait";
    w.setAttribute("role", "status");
    w.appendChild(tpl.content.cloneNode(true));
    w.appendChild(document.createTextNode(say));
    at.insertAdjacentElement("afterend", w);
    setTimeout(function () { if (at.tagName === "BUTTON") at.disabled = true; }, 0);
  });
  // Settings: a form marked data-savebar keeps its Save bar out of the way until something in
  // it changes, and again after Revert. Without this script the bar simply stays. A field that
  // belongs to another form (form="…", Find my plan's search) does not count.
  document.querySelectorAll("form[data-savebar]").forEach(function (f) {
    f.classList.add("sb-js");
    function dirty(e) { if (!e.target.form || e.target.form === f) f.classList.add("sb-dirty"); }
    f.addEventListener("input", dirty);
    f.addEventListener("change", dirty);
    f.addEventListener("reset", function () { f.classList.remove("sb-dirty"); });
  });
})();

// A place name anywhere (a `.pn`: filters.place_refs, a map popup) opens the naming card
// beside it. The card's script (placecard.js) and markup come on the first tap, never with
// the page. Inside a list row the name is a span acting as a button, so its tap must not
// follow the row's link: the capture phase takes it first.
(function () {
  var me = document.currentScript, v = me && /[?&]v=([^&]+)/.exec(me.src), loading = null;
  function card() {
    if (window.CCPlaceCard) return Promise.resolve(window.CCPlaceCard);
    if (!loading) {
      loading = new Promise(function (ok, no) {
        var m = document.querySelector('meta[name="cc-root"]'), s = document.createElement("script");
        s.src = ((m && m.content) || "") + "/static/placecard.js" + (v ? "?v=" + v[1] : "");
        s.onload = function () { ok(window.CCPlaceCard); };
        s.onerror = function () { loading = null; no(new Error("placecard.js")); };
        document.head.appendChild(s);
      });
    }
    return loading;
  }
  window.ccPlaceCard = card; // the Places page opens it on its own rows and markers
  function go(el) {
    card().then(function (c) { if (c) c.open({ lat: +el.dataset.lat, lon: +el.dataset.lon, anchor: el }); }).catch(function () {});
  }
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest(".pn[data-lat]");
    if (!el) return;
    e.preventDefault();
    e.stopPropagation();
    go(el);
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var el = e.target;
    if (!el.matches || !el.matches("span.pn[data-lat]")) return;
    e.preventDefault();
    e.stopPropagation();
    go(el);
  }, true);
})();

// The live Now page: a page whose <main> names a stream gets each card swapped in place when
// what it shows changes. The stream rests while the tab is hidden; reopening it sends every
// card again, so a page brought back is current at once.
(function () {
  var main = document.querySelector("main[data-live-src]");
  if (!main || !window.EventSource) return;
  var src = main.getAttribute("data-live-src"), es = null;
  function swap(e) {
    var c;
    try { c = JSON.parse(e.data); } catch (_) { return; }
    var old = document.querySelector('[data-live="' + CSS.escape(c.vin) + '"]');
    if (!old) return;
    var t = document.createElement("template");
    t.innerHTML = c.html.trim();
    var fresh = t.content.firstElementChild;
    // the same cars doing the same things (the Now banners): only the words change, so the scene keeps moving
    var sig = fresh && fresh.getAttribute("data-sig");
    if (sig && sig === old.getAttribute("data-sig")) {
      var to = old.querySelectorAll("[data-fill]"), from = fresh.querySelectorAll("[data-fill]");
      if (to.length === from.length) {
        from.forEach(function (f, i) { if (to[i].innerHTML !== f.innerHTML) to[i].innerHTML = f.innerHTML; });
        document.dispatchEvent(new CustomEvent("cc:live", { detail: c }));
        return;
      }
    }
    if (fresh && fresh.outerHTML !== old.outerHTML) old.replaceWith(fresh);
    document.dispatchEvent(new CustomEvent("cc:live", { detail: c }));
  }
  function open() {
    if (es) return;
    es = new EventSource(src);
    es.addEventListener("card", swap);
    es.addEventListener("open", function () { try { sessionStorage.removeItem("cc-live-reload"); } catch (_) {} });
    // A refused stream (a sign-in that ran out) closes for good, and the page would go quietly
    // stale: reload once, so the sign-in shows; never twice in a row.
    es.addEventListener("error", function () {
      if (!es || es.readyState !== EventSource.CLOSED) return;
      var again = false;
      try { again = !sessionStorage.getItem("cc-live-reload"); sessionStorage.setItem("cc-live-reload", "1"); } catch (_) {}
      if (again) location.reload();
    });
  }
  function close() {
    if (es) { es.close(); es = null; }
  }
  // a hidden tab stops the banners' animation too (html.cc-hidden, style.css), not only the stream
  document.addEventListener("visibilitychange", function () {
    document.documentElement.classList.toggle("cc-hidden", document.hidden);
    if (document.hidden) close(); else open();
  });
  window.addEventListener("pagehide", close);
  if (!document.hidden) open();
})();

// Address suggestions: an address field marked data-suggest offers what the local maps
// add-on's own index matches as you type (the site's /places/search, which asks nothing
// else). Debounced; a slow answer for older text never replaces a newer one; the list
// floats over what is below it, so nothing moves; arrows, Enter and Escape work. Picking
// one fills the field and tells the page where it is (a "cc:suggestion" event).
(function () {
  var seq = 0, timer = null;
  function root() { var m = document.querySelector('meta[name="cc-root"]'); return (m && m.content) || ""; }
  function listOf(input) { return document.getElementById(input.getAttribute("aria-controls")); }
  function near(input) {
    if (window.CCPlaceMap) return window.CCPlaceMap.center();
    var f = input.form, la = f && f.querySelector('input[name="lat"],input[name="latitude"]'), lo = f && f.querySelector('input[name="lon"],input[name="longitude"]');
    return la && lo && la.value && lo.value ? { lat: +la.value, lon: +lo.value } : null;
  }
  function close(input) {
    var list = listOf(input);
    if (!list) return;
    list.hidden = true;
    list.innerHTML = "";
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }
  function active(input, step) {
    var list = listOf(input), opts = list ? Array.prototype.slice.call(list.querySelectorAll('[role="option"]')) : [];
    if (!opts.length) return null;
    var i = opts.findIndex(function (o) { return o.getAttribute("aria-selected") === "true"; });
    if (step) {
      i = i < 0 ? (step > 0 ? 0 : opts.length - 1) : (i + step + opts.length) % opts.length;
      opts.forEach(function (o, j) { o.setAttribute("aria-selected", j === i ? "true" : "false"); });
      input.setAttribute("aria-activedescendant", opts[i].id);
      opts[i].scrollIntoView({ block: "nearest" });
    }
    return i < 0 ? null : opts[i];
  }
  function pick(input, opt) {
    input.value = opt.dataset.text;
    close(input);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new CustomEvent("cc:suggestion", { bubbles: true, detail: { lat: +opt.dataset.lat, lon: +opt.dataset.lon, label: opt.dataset.text } }));
  }
  function show(input, data) {
    var list = listOf(input);
    if (!list) return;
    list.innerHTML = "";
    var rows = (data && data.results) || [];
    if (data && data.available === false) {
      var li = document.createElement("li");
      li.className = "sugg-note";
      li.textContent = "No suggestions: the local maps add-on isn’t ready.";
      list.appendChild(li);
    }
    rows.forEach(function (r, i) {
      var li = document.createElement("li"), b = document.createElement("span"), m = document.createElement("span");
      li.id = list.id + "-" + i;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.dataset.lat = r.lat;
      li.dataset.lon = r.lon;
      li.dataset.text = r.city ? r.label + ", " + r.city : r.label;
      b.textContent = r.label;
      m.className = "muted";
      m.textContent = [r.city, r.kind === "address" || r.kind === "street" ? "" : String(r.kind || "").replace(/_/g, " ")].filter(Boolean).join(" · ");
      li.appendChild(b);
      li.appendChild(m);
      list.appendChild(li);
    });
    var open = list.children.length > 0;
    list.hidden = !open;
    input.setAttribute("aria-expanded", open ? "true" : "false");
  }
  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!input.matches || !input.matches("input[data-suggest]") || !e.isTrusted) return;
    clearTimeout(timer);
    var q = input.value.trim(), mine = ++seq;
    if (q.length < 3) { close(input); return; }
    timer = setTimeout(function () {
      var at = near(input), url = root() + "/places/search?q=" + encodeURIComponent(q) + (at ? "&lat=" + at.lat.toFixed(5) + "&lon=" + at.lon.toFixed(5) : "");
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (mine === seq) show(input, data); })
        .catch(function () { if (mine === seq) close(input); });
    }, 250);
  });
  document.addEventListener("keydown", function (e) {
    var input = e.target;
    if (!input.matches || !input.matches("input[data-suggest]")) return;
    var list = listOf(input), open = list && !list.hidden;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { if (open) { e.preventDefault(); active(input, e.key === "ArrowDown" ? 1 : -1); } }
    else if (e.key === "Enter") { var o = open && active(input, 0); if (o) { e.preventDefault(); pick(input, o); } }
    else if (e.key === "Escape") { if (open) { e.preventDefault(); e.stopPropagation(); close(input); } }
  });
  document.addEventListener("mousedown", function (e) {
    var opt = e.target.closest && e.target.closest('.sugg-list [role="option"]');
    if (!opt) return;
    e.preventDefault(); // keep the field's focus: the pick happens before any blur
    var input = document.querySelector('input[aria-controls="' + opt.parentElement.id + '"]');
    if (input) pick(input, opt);
  });
  document.addEventListener("focusout", function (e) {
    var input = e.target;
    if (input.matches && input.matches("input[data-suggest]")) setTimeout(function () { if (document.activeElement !== input) close(input); }, 150);
  });
})();

// A place's size as a slider and a typed number, kept together both ways (a place's page,
// and anywhere else that sizes a place). `flag` is the form's "resized" field: it is set only
// when the size really changes, by the slider or by a typed number that differs from the
// size now, so a place nobody resized is never resized, nor one bigger than the slider's
// range clamped. Typing waits for a number in range; leaving the field (or Enter) clamps it,
// or puts the size back when it is not a number. `onSize(m, done)` draws it.
(function () {
  function sizeTyped(text, cur, min, max, commit) {
    var v = Math.round(parseFloat(text));
    if (!isFinite(v)) return { v: cur, changed: false, show: commit ? cur : null };
    if (v === cur) return { v: cur, changed: false, show: null }; // as it was: even 1500 m stays
    if (v < min || v > max) {
      if (!commit) return { v: cur, changed: false, show: null };
      v = Math.min(max, Math.max(min, v));
    }
    return { v: v, changed: v !== cur, show: commit ? v : null };
  }
  function bind(range, number, flag, onSize) {
    var min = +number.dataset.min || +range.min, max = +number.dataset.max || +range.max;
    var cur = Math.round(+number.value); // the size as stored, even outside the slider's range
    function set(v, done) {
      cur = v;
      if (flag) flag.value = "1";
      if (onSize) onSize(v, done);
    }
    range.addEventListener("input", function () { number.value = range.value; set(+range.value, false); });
    range.addEventListener("change", function () { if (onSize) onSize(cur, true); });
    function typed(commit) {
      var t = sizeTyped(number.value, cur, min, max, commit);
      if (t.show !== null && String(t.show) !== number.value) number.value = t.show;
      if (!t.changed) return;
      range.value = t.v;
      set(t.v, commit);
    }
    number.addEventListener("input", function () { typed(false); });
    number.addEventListener("change", function () { typed(true); });
    // Enter in the field sends the form: what it says then is the size, clamped
    if (number.form) number.form.addEventListener("submit", function () { typed(true); }, true);
  }
  window.CCSize = { bind: bind, sizeTyped: sizeTyped };
})();

// The (i) notes: one open at a time, and a tap anywhere else, a tap on the note itself or
// Escape closes it. On a phone the note sits at the foot of the screen, far from its (i).
(function () {
  function closeAll(except) {
    document.querySelectorAll("details.info[open]").forEach(function (d) { if (d !== except) d.removeAttribute("open"); });
  }
  document.addEventListener("click", function (e) {
    var tip = e.target.closest && e.target.closest("details.info > .tip");
    if (tip) { tip.parentElement.removeAttribute("open"); return; }
    var own = e.target.closest && e.target.closest("details.info");
    closeAll(own);
  });
  document.addEventListener("toggle", function (e) {
    if (e.target.matches && e.target.matches("details.info") && e.target.open) closeAll(e.target);
  }, true);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeAll(null); });
})();

// The Powerwall row on Now: drawn again every 20 s while the page is in view, from the site's own
// /energy/row (the same markup as the page, from the newest reading in memory). A row that has
// gone stale comes back empty and goes; one that was not there is not added.
(function () {
  function root() { var m = document.querySelector('meta[name="cc-root"]'); return (m && m.content) || ""; }
  function tick() {
    var row = document.querySelector(".pwrow");
    if (!row || document.hidden) return;
    fetch(root() + "/energy/row", { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) return;
        var now = document.querySelector(".pwrow");
        if (!now) return;
        var holder = document.createElement("div");
        holder.innerHTML = html.trim();
        var next = holder.firstElementChild;
        if (next) now.replaceWith(next); else now.remove();
      })
      .catch(function () {});
  }
  if (document.querySelector(".pwrow")) setInterval(tick, 20000);
})();

// The Powerwall history backfill's bar: redrawn in place every few seconds from the site's own
// progress answer while it runs, with no reload. It stops when the run is over, and while the tab
// is hidden (it looks again when the tab comes back).
(function () {
  var row = document.querySelector("[data-bf]");
  if (!row) return;
  var timer = null, url = row.getAttribute("data-bf");
  function paint(d) {
    row.setAttribute("data-state", d.state);
    var text = row.querySelector(".bf-text");
    if (text) text.textContent = d.error && d.state === "error" ? d.error : d.text || text.textContent;
    var bar = document.querySelector(".bf-bar"), stop = row.querySelector(".bf-stop");
    if (bar) { bar.hidden = d.state !== "running"; var p = bar.querySelector("progress"), n = bar.querySelector(".bf-pct"); if (p) p.value = d.percent; if (n) n.textContent = d.percent + "%"; }
    if (stop) stop.hidden = d.state !== "running";
  }
  function halt() { if (timer) clearInterval(timer); timer = null; }
  function tick() {
    if (document.hidden) return;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (!d) return; paint(d); if (d.state !== "running") halt(); })
      .catch(function () {});
  }
  function begin() { if (!timer && row.getAttribute("data-state") === "running") timer = setInterval(tick, 4000); }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) halt();
    else if (row.getAttribute("data-state") === "running") { tick(); begin(); }
  });
  begin();
})();

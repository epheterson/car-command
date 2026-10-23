// The chart: one filled line, any of its extra lines switched on beside it, a lane underneath
// saying what the car was doing, and a card that follows the pointer or finger with every
// line's figure at that moment. It draws every .chartbox on the page from the JSON block
// inside it (chartdata.payload) and needs no library. Everything a person reads goes in with
// textContent, never as markup.
//
// Other parts of the page can follow it: the box fires "cc:chart-t" ({t}, or {t: null} when
// the pointer leaves) and carries box.ccChart = {show(t), rest()}; boxes sharing a
// data-sync group follow each other's pointer. A payload with `line` joins its points straight
// (a series of estimates) where readings otherwise step.
(function () {
  var NS = "http://www.w3.org/2000/svg";
  var WORD = { asleep: "Asleep", parked: "Parked", caron: "Car on, parked", driving: "Driving", charging: "Charging", unknown: "Not logged" };
  var SINCE = { drive: "since leaving", charge: "since plug-in" };
  // the figures whose change since the start means something; a change in volts or the outside
  // temperature since plug-in is noise, and "0°F" reads as a temperature
  var DELTA = { level: 1, range: 1, elevation: 1, full: 1 };

  function draw(box) {
    var data;
    try { data = JSON.parse(box.querySelector(".chart-data").textContent); } catch (e) { return; }
    var P = data.primary;
    if (!P || !P.points || !P.points.length) return;
    box.classList.toggle("p-level", P.key === "level"); // gold is the battery's line alone
    var svg = box.querySelector(".chart-svg"), hov = box.querySelector(".chart-hov"), area = box.querySelector(".chart-area");
    var EX = data.extras || [], marks = data.marks || [], rug = data.rug || [], bands = data.bands || [], dist = data.xkind === "dist";
    EX.forEach(function (x, i) { x.tone = x.tone || "x" + (i % 4); });
    // a line in the primary's own unit is drawn on the primary's scale, with no axis of its own
    EX.forEach(function (x) { if (x.unit === P.unit) { x.lo = P.lo; x.hi = P.hi; x.shared = true; } });
    // the extra lines switched on, oldest first: the right-hand scale is the newest one's
    // (a link may carry its own: the lines switched on when it was copied, static/view.js)
    var keys = data.on || [], shown = window.CCView && CCView.chartFrom(box.id, EX.map(function (x) { return x.key; }));
    var on = shown ? shown.map(function (k) { return EX.filter(function (x) { return x.key === k; })[0]; }) : EX.filter(function (x) { return keys.indexOf(x.key) >= 0; });  // a link's order is the order they were chosen in
    if (window.CCView) CCView.chart(box.id, on.map(function (x) { return x.key; }), keys, false);
    var W, H, L = 34, R = 44, T = marks.length || rug.length ? 22 : 14, PH, LANE_Y, LANE_H = 10, PW, narrow, G = {};
    // a month of readings (Battery, the odometer) has no lane of states and no events to name
    var hasLane = (data.lane || []).length > 0 || (data.events || []).length > 0;
    // an axis label: the figure with its thousands, the unit (with its space) on the top one only
    function axis(s, v, i) { return Math.round(v).toLocaleString() + (i === 2 ? s.unit : ""); }
    // an optional profile under the lane on the same time axis (a drive's elevation): data.strip
    var SP = data.strip && data.strip.points && data.strip.points.length ? data.strip : null, SH = SP ? 30 : 0, SY;
    // the margins fit the widest axis label, so "388 mi" or "1,000 V" is not cut off
    function widest(s) { return Math.max.apply(null, [s.lo, (s.lo + s.hi) / 2, s.hi].map(function (v, i) { return axis(s, v, i).length; })); }
    L = Math.max(34, Math.round(12 + 6.4 * widest(P)));
    // the strip's own labels are a size smaller (.tk-s), and the margin fits them too
    if (SP) L = Math.max(L, Math.round(12 + 5.8 * (Math.round(SP.hi).toLocaleString() + SP.unit).length));
    R = Math.max(EX.some(function (x) { return !x.shared; }) ? 44 : 16, Math.round(12 + 6.4 * Math.max.apply(null, [0].concat(EX.filter(function (x) { return !x.shared; }).map(widest)))));
    var end = dist ? data.t1 : Math.min(data.t1, Date.now() / 1000);
    // the card's time on the axis's clock: the site's zone (data.tz), not the browser's
    // and the clock Settings chose (data.h12), not the browser's
    var when, h12 = window.CCClock ? CCClock.hour(data.h12) : {};
    try { when = new Intl.DateTimeFormat(undefined, Object.assign({ weekday: "short", month: "short", day: "numeric", minute: "2-digit", timeZone: data.tz || undefined }, h12)); }
    catch (e) { when = new Intl.DateTimeFormat(undefined, Object.assign({ weekday: "short", month: "short", day: "numeric", minute: "2-digit" }, h12)); }

    function layout() {
      narrow = area.clientWidth < 520;
      W = narrow ? Math.max(280, Math.round(area.clientWidth)) : 680;
      H = (narrow ? 290 : 250) + T - 14 + (SP ? SH + 10 + SG() : 0);
      // a lane only where it says something: one drive or one charge is one state, and the card names it
      LANE_H = (data.lane || []).length ? 10 : 0;
      PH = (narrow ? 210 : 170) + (LANE_H ? 0 : 20);
      LANE_Y = T + PH + (LANE_H ? 10 : 0);
      SY = LANE_Y + LANE_H + 8 + SG();
      PW = W - L - R;
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      area.classList.toggle("narrow", narrow);
    }
    // with no lane between them, the strip stands off the chart so its top label clears the axis's bottom one
    function SG() { return (data.lane || []).length ? 0 : 12; }
    function X(t) { return L + (t - data.t0) / (data.t1 - data.t0) * PW; }
    function Tx(x) { return data.t0 + (x - L) / PW * (data.t1 - data.t0); }
    function Y(s, v) { var c = Math.min(Math.max(v, s.lo), s.hi); return T + PH - (c - s.lo) / (s.hi - s.lo) * PH; }
    function sY(v) { return SY + SH - (Math.min(Math.max(v, SP.lo), SP.hi) - SP.lo) / ((SP.hi - SP.lo) || 1) * SH; }
    function el(n, a, p) { var e = document.createElementNS(NS, n); for (var k in a) e.setAttribute(k, a[k]); (p || svg).appendChild(e); return e; }
    function tx(x, y, s, cls, anchor, p) { var e = el("text", { x: x, y: y, "class": cls, "text-anchor": anchor || "start" }, p); e.textContent = s; return e; }
    function at(pts, t) {
      if (!pts.length) return null;
      if (t <= pts[0][0]) return pts[0][1];
      var lo = 0, hi = pts.length - 1;
      while (lo < hi) { var m = (lo + hi + 1) >> 1; if (pts[m][0] <= t) lo = m; else hi = m - 1; }
      return pts[lo][1];
    }
    // where a line is at t, for its dot: straight between readings for a sampled series or a
    // chart of estimates; the card still reads the point itself
    function onLine(s, t) {
      var pts = s.points, v = at(pts, t);
      if (!(data.line || s.draw === "line") || v === null) return v;
      for (var i = 1; i < pts.length; i++) if (pts[i][0] >= t) { var a = pts[i - 1], f = (t - a[0]) / ((pts[i][0] - a[0]) || 1); return t < a[0] ? v : a[1] + (pts[i][1] - a[1]) * f; }
      return v;
    }
    function idx(pts, t) { var lo = 0, hi = pts.length - 1; while (lo < hi) { var m = (lo + hi + 1) >> 1; if (pts[m][0] <= t) lo = m; else hi = m - 1; } return lo; }
    // a sampled series (speed, power, height) is a line through its readings, broken where the
    // log has a gap; a held one (battery %, a charger's setting) is steps
    function trace(s) {
      if (s.draw !== "line") return step(s);
      var d = "", prev = null, gap = s.gap || 300;
      s.points.forEach(function (p) {
        d += (prev === null || p[0] - prev > gap ? "M" : "L") + X(p[0]).toFixed(1) + "," + Y(s, p[1]).toFixed(1);
        prev = p[0];
      });
      return d;
    }
    // the area under a trace, closed down to the axis run by run
    function under(s, base) {
      if (s.draw !== "line") { var st = step(s); return st + "L" + X(end) + "," + base + "L" + X(s.points[0][0]) + "," + base + "Z"; }
      return trace(s).split("M").filter(Boolean).map(function (run) {
        var xs = run.split("L"), x0 = xs[0].split(",")[0], x1 = xs[xs.length - 1].split(",")[0];
        return "M" + run + "L" + x1 + "," + base + "L" + x0 + "," + base + "Z";
      }).join("");
    }
    function step(s) {
      var d = "", prev = null;
      s.points.forEach(function (p) {
        var x = X(p[0]).toFixed(1), y = Y(s, p[1]).toFixed(1);
        // readings hold until the next one, so they step; a series of estimates (`line`) is joined straight
        d += prev === null ? "M" + x + "," + y : (data.line ? "L" + x + "," + y : "L" + x + "," + prev + "L" + x + "," + y);
        prev = y;
      });
      return d + (data.line ? "" : "L" + X(end).toFixed(1) + "," + prev);
    }
    // heights in whole feet or metres: a tenth of a foot is GPS noise
    function round(s, v) { var k = s.key === "elevation" ? 1 : 10; return Math.round(v * k) / k; }
    function num(v) { return (Math.round(v * 10) / 10).toLocaleString(); }
    function fmt(s, v) { return v === null ? "—" : round(s, v).toLocaleString() + s.unit; }
    function delta(s, v) { var r = round(s, v); return r === 0 ? "0" + s.unit : (r > 0 ? "+" : "−") + Math.abs(r).toLocaleString() + s.unit; }
    function hm(sec) { var m = Math.round(sec / 60); if (m < 60) return m + " min"; var h = Math.floor(m / 60); m = m % 60; return h + " h" + (m ? " " + m + " min" : ""); }
    // a label that would touch the one before it is left out ("Jul 8Jul 22" on a phone)
    function labels(list, y, cls, lineFrom) {
      var lastRight = -Infinity;
      list.forEach(function (k) {
        var x = X(k[0]), w = String(k[1]).length * 6.2, a = x - L < 14 ? "start" : (L + PW - x < 14 ? "end" : "middle");
        var left = a === "start" ? x : a === "end" ? x - w : x - w / 2;
        if (lineFrom) lineFrom(k, x);
        if (left < lastRight + 8) return;
        tx(x, y, k[1], cls, a);
        lastRight = left + w;
      });
    }

    function render() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      var gid = box.id + "-fill", defs = el("defs", {}), grad = el("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
      el("stop", { offset: "0%", "class": "g0" }, grad);
      el("stop", { offset: "100%", "class": "g1" }, grad);
      // shaded spans first, so every line is drawn over them
      bands.forEach(function (b) {
        var x0 = X(Math.max(b[0], data.t0)), x1 = X(Math.min(b[1], data.t1));
        if (x1 > x0) el("rect", { x: x0, y: T, width: Math.max(1, x1 - x0), height: PH, "class": "bd bd-" + b[2] });
      });
      [P.lo, (P.lo + P.hi) / 2, P.hi].forEach(function (v, i) {
        el("line", { x1: L, x2: L + PW, y1: Y(P, v), y2: Y(P, v), "class": "grid" });
        tx(L - 6, Y(P, v) + 3.5, axis(P, v, i), "tk", "end");
      });
      // its grid line stays when the label is left out
      labels(data.ticks || [], H - 6, "tk", function (k, x) { if (k[2]) el("line", { x1: x, x2: x, y1: T, y2: LANE_Y + LANE_H, "class": "day" }); });
      labels(marks, T - 7, "tk mk-t", function (k, x) { el("line", { x1: x, x2: x, y1: T - 3, y2: T + PH, "class": "mk" }); });
      // a rug: a short tick on the top edge for each of many moments (software updates), named on hover
      rug.forEach(function (k) { var x = X(k[0]); el("line", { x1: x, x2: x, y1: T - 4, y2: T + 4, "class": "rug" }); });
      G.rugHi = el("line", { y1: T, y2: T + PH, "class": "mk rug-hi" });
      // a line of estimates on a cropped axis has no fill: a fill reads as an amount from zero
      if (!data.line) el("path", { d: under(P, T + PH), fill: "url(#" + gid + ")" });
      el("path", { d: trace(P), "class": "l-primary" });
      if (data.avg) {
        var ay = Y(P, data.avg.value);
        el("line", { x1: L, x2: L + PW, y1: ay, y2: ay, "class": "avg" });
        tx(L + PW - 4, ay - 5, data.avg.label + " " + fmt(P, data.avg.value), "tk avg-t", "end");
      }
      G.extra = el("g", {});  // after the fill, so the extra lines are not tinted by it
      if (LANE_H) {
        el("rect", { x: L, y: LANE_Y, width: PW, height: LANE_H, rx: 3, "class": "lane-bg" });
        (data.lane || []).forEach(function (s) {
          el("rect", { x: X(s[0]), y: LANE_Y, width: Math.max(s[2] === "driving" ? 2 : 0.6, X(s[1]) - X(s[0])), height: LANE_H, "class": "ln-" + s[2] });
        });
      }
      if (SP) {
        var sd = "", sprev = null;
        SP.points.forEach(function (p) { sd += (sprev === null || p[0] - sprev > (SP.gap || 300) ? "M" : "L") + X(p[0]).toFixed(1) + "," + sY(p[1]).toFixed(1); sprev = p[0]; });
        var sg = el("g", { "class": "s-" + (SP.tone || SP.key) });
        sd.split("M").filter(Boolean).forEach(function (run) {
          var xs = run.split("L"), x0 = xs[0].split(",")[0], x1 = xs[xs.length - 1].split(",")[0];
          el("path", { d: "M" + run + "L" + x1 + "," + (SY + SH) + "L" + x0 + "," + (SY + SH) + "Z", "class": "strip-a" }, sg);
        });
        el("path", { d: sd, "class": "strip-l" }, sg);
        tx(L - 6, SY + 8, Math.round(SP.hi).toLocaleString() + SP.unit, "tk tk-s", "end");
        tx(L - 6, SY + SH, Math.round(SP.lo).toLocaleString(), "tk tk-s", "end");
        SP.dot = el("circle", { r: 3, "class": "dot2 s-" + (SP.tone || SP.key) });
      }
      G.cross = el("line", { y1: T, y2: SP ? SY + SH : LANE_Y + LANE_H, "class": "cross" });
      G.dot = el("circle", { r: 4.5, "class": "dotl" });
      G.dots = el("g", {});
      drawExtra();
    }
    function drawExtra() {
      [G.extra, G.dots].forEach(function (g) { while (g.firstChild) g.removeChild(g.firstChild); });
      on.forEach(function (x) {
        if (!x.points.length) return;
        var g = el("g", { "class": "s-" + x.tone }, G.extra);
        el("path", { d: trace(x), "class": "l-extra" }, g);
        x.dot = el("circle", { r: 3.5, "class": "dot2 s-" + x.tone }, G.dots);
      });
      // one right-hand scale, the newest line's: two or three side by side would take the
      // plot's width on a phone, and the card gives every line's exact figure anyway
      var top = on.filter(function (x) { return !x.shared; }).pop();
      if (!top || !top.points.length) return;
      var g = el("g", { "class": "s-" + top.tone }, G.extra);
      [top.lo, (top.lo + top.hi) / 2, top.hi].forEach(function (v, i) {
        tx(L + PW + 6, Y(top, v) + 3.5, axis(top, v, i), "tk t-extra", "start", g);
      });
    }
    function eventAt(t) { var ev = data.events || []; for (var i = 0; i < ev.length; i++) if (t >= ev[i].a && t <= ev[i].b) return ev[i]; return null; }
    function laneAt(t) { var ln = data.lane || []; for (var i = 0; i < ln.length; i++) if (t >= ln[i][0] && t <= ln[i][1]) return ln[i]; return null; }
    function bandAt(t) { for (var i = 0; i < bands.length; i++) if (bands[i][3] && t >= bands[i][0] && t <= bands[i][1]) return bands[i]; return null; }
    // the mark under the pointer: within a few pixels of it
    function rugAt(t) { var best = null; rug.forEach(function (m) { var d = Math.abs(X(m[0]) - X(t)); if (d <= 4 && (!best || d < best[0])) best = [d, m]; }); return best && best[1]; }
    function markAt(t) { var best = null; marks.forEach(function (m) { var d = Math.abs(X(m[0]) - X(t)); if (d <= 6 && (!best || d < best[0])) best = [d, m]; }); return best && best[1]; }
    function node(tag, cls, text, parent) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; if (parent) parent.appendChild(e); return e; }
    function card(t) {
      while (hov.firstChild) hov.removeChild(hov.firstChild);
      var ev = eventAt(t), ln = laneAt(t), bd = bandAt(t), mk = markAt(t);
      var kind = ev ? (ev.kind === "drive" ? "driving" : "charging") : (ln ? ln[2] : "unknown");
      var nm = P.names ? P.names[idx(P.points, t)] : null;
      node("div", "hv-when", nm || (dist ? num(t) + (data.xunit || "") : when.format(new Date(t * 1000))), hov);
      if (hasLane) {
        var st = node("div", "hv-state", null, hov);
        node("i", "k-" + kind, null, st);
        node("span", null, WORD[kind] + (ev && ev.title ? " · " + ev.title : "") + (ev ? " · " + hm(t - ev.a) + " in" : ""), st);
      }
      // every line's figure, the ones drawn first; inside a drive or charge, each one's change since it began
      var figs = node("div", "hv-figs", null, hov), rows = [P].concat(on, SP ? [SP] : [], EX.filter(function (x) { return on.indexOf(x) < 0; }));
      if (ev) {
        var h = node("div", "hv-f hv-h", null, figs);
        node("i", null, null, h); node("span", null, null, h); node("b", null, null, h);
        node("em", null, SINCE[ev.kind] || "since start", h);
      }
      rows.forEach(function (s) {
        var v = at(s.points, t), f = node("div", "hv-f " + (s === P ? "hv-p" : "s-" + (s.tone || s.key) + (on.indexOf(s) < 0 && s !== SP ? " off" : "")), null, figs);
        node("i", null, null, f);
        node("span", null, s.label, f);
        node("b", null, fmt(s, v), f);
        var a = ev ? at(s.points, ev.a) : null;
        node("em", null, ev && v !== null && a !== null && DELTA[s.key] ? delta(s, v - a) : "", f);
      });
      var ctx = [];
      if (mk) ctx.push(mk[1]);
      var rg = rugAt(t);
      if (G.rugHi) { G.rugHi.setAttribute("x1", rg ? X(rg[0]) : -10); G.rugHi.setAttribute("x2", rg ? X(rg[0]) : -10); }
      if (rg) ctx.push(rg[1]);
      if (bd) ctx.push(bd[3]);
      if (ev && ev.figs) ctx.push(ev.figs);
      else if (ln && !ev) {
        var a0 = at(P.points, ln[0]), b0 = at(P.points, Math.min(ln[1], end));
        ctx.push(WORD[kind] + " " + hm(Math.min(ln[1], end) - ln[0]) + (a0 !== null && b0 !== null && a0 - b0 >= 0.5 ? " · lost " + Math.round(a0 - b0) + P.unit : ""));
      }
      if (ctx.length) node("div", "hv-ctx", ctx.join(" · "), hov);
      if (narrow) { hov.style.left = ""; hov.style.top = ""; return; }
      var r = area.getBoundingClientRect(), px = X(t) / W * r.width, v0 = at(P.points, t), py = Y(P, v0 === null ? P.lo : v0) / H * r.height;
      // the card goes on the side of the pointer with more room, and stays inside the chart
      var cw = hov.offsetWidth || 260, ch = hov.offsetHeight || 120, left = px > r.width / 2 ? px - cw - 16 : px + 16, top = py - ch / 2;
      // a line read across its whole width: the card keeps to the half away from the pointer, at the top
      if (data.line) { left = px > r.width / 2 ? 8 : r.width - cw - 8; top = 4; }
      hov.style.left = Math.max(4, Math.min(left, r.width - cw - 4)) + "px";
      hov.style.top = Math.max(0, Math.min(top, r.height - ch)) + "px";
    }
    function show(t) {
      var x = X(t), v = onLine(P, t);
      G.cross.setAttribute("x1", x);
      G.cross.setAttribute("x2", x);
      G.dot.setAttribute("cx", x);
      G.dot.setAttribute("cy", Y(P, v === null ? P.lo : v));
      on.forEach(function (s) { if (!s.dot) return; var e = onLine(s, t); s.dot.setAttribute("cx", x); s.dot.setAttribute("cy", Y(s, e === null ? s.lo : e)); });
      if (SP && SP.dot) { var e2 = at(SP.points, t); SP.dot.setAttribute("cx", x); SP.dot.setAttribute("cy", sY(e2 === null ? SP.lo : e2)); }
      card(t);
    }
    function tFrom(ev) {
      var p = ev.touches ? ev.touches[0] : ev, r = svg.getBoundingClientRect();
      return Math.min(Tx(Math.min(Math.max((p.clientX - r.left) / r.width * W, L), L + PW)), end);
    }
    function emit(t) {
      var e;
      try { e = new CustomEvent("cc:chart-t", { bubbles: true, detail: { t: t } }); } catch (x) { return; }
      box.dispatchEvent(e);
    }

    layout();
    render();
    var evs = data.events || [];
    var longest = evs.reduce(function (a, b) { return !a || (b.b - b.a) > (a.b - a.a) ? b : a; }, null);
    var resting = longest ? (longest.a + Math.min(longest.b, end)) / 2 : P.points[P.points.length - 1][0];
    var last = resting, linger = 0, touched = 0;
    // At rest nothing floats over the data: the crosshair, dots and card show only while a
    // pointer or finger is on the chart. The phone's card is a static panel under the chart,
    // so it keeps the resting summary (CSS hides only the wide screen's floating card).
    function settle() { clearTimeout(linger); last = resting; show(resting); box.classList.add("resting"); }
    function rest() { settle(); emit(null); }
    function hold(t) { clearTimeout(linger); box.classList.remove("resting"); last = t; show(t); }
    function move(ev) { hold(tFrom(ev)); emit(last); }
    // A tap is followed by the browser's own mousemove; ignore it so the linger runs out.
    function touch(ev) { touched = Date.now(); move(ev); }
    settle();
    svg.addEventListener("mousemove", function (ev) { if (Date.now() - touched > 800) move(ev); });
    svg.addEventListener("touchstart", touch, { passive: true });
    svg.addEventListener("touchmove", touch, { passive: true });
    svg.addEventListener("touchend", function () { touched = Date.now(); clearTimeout(linger); linger = setTimeout(rest, 2500); }, { passive: true });
    svg.addEventListener("mouseleave", function () { rest(); });
    // A click or tap on the chart picks that moment ("cc:chart-pick"), for a page that acts on one (the drive page's Split).
    svg.addEventListener("click", function (ev) {
      var e;
      try { e = new CustomEvent("cc:chart-pick", { bubbles: true, detail: { t: tFrom(ev) } }); } catch (x) { return; }
      box.dispatchEvent(e);
    });
    window.addEventListener("resize", function () { var was = narrow, w = W; layout(); if (was !== narrow || (narrow && w !== W)) render(); show(last); });
    // Driven from outside (a second chart, a map): it shows the moment without firing
    // cc:chart-t again, so two followers never chase each other. A time off this chart rests it.
    box.ccChart = {
      show: function (t) { if (t === null || t === undefined || t < data.t0 || t > end) settle(); else hold(t); },
      rest: settle
    };
    box.querySelectorAll("[data-extra]").forEach(function (b) {
      var x = null;
      EX.forEach(function (e) { if (e.key === b.getAttribute("data-extra")) x = e; });
      if (!x) return;
      function mark() { var s = on.indexOf(x) >= 0; b.classList.toggle("on", s); b.setAttribute("aria-pressed", s ? "true" : "false"); }
      mark();
      b.addEventListener("click", function () {
        var i = on.indexOf(x);
        if (i >= 0) on.splice(i, 1); else on.push(x);
        if (window.CCView) CCView.chart(box.id, on.map(function (e) { return e.key; }), keys, true);
        mark();
        drawExtra();
        show(last);
      });
    });
  }

  // boxes in the same data-sync group follow each other's pointer
  document.addEventListener("cc:chart-t", function (e) {
    var from = e.target, group = from.getAttribute && from.getAttribute("data-sync");
    if (!group) return;
    document.querySelectorAll(".chartbox[data-sync]").forEach(function (b) {
      if (b !== from && b.getAttribute("data-sync") === group && b.ccChart) b.ccChart.show(e.detail.t);
    });
  });
  function all() { document.querySelectorAll(".chartbox").forEach(draw); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all); else all();
})();

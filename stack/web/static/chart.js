// The chart: one filled line, a lane underneath saying what the car was doing, and a
// card that follows the pointer or finger with that moment's figures. It draws every
// .chartbox on the page from the JSON block inside it (chartdata.payload) and needs no
// library. Everything a person reads goes in with textContent, never as markup.
(function () {
  var NS = "http://www.w3.org/2000/svg";
  var WORD = { asleep: "Asleep", parked: "Parked", driving: "Driving", charging: "Charging", unknown: "Not logged" };

  function draw(box) {
    var data;
    try { data = JSON.parse(box.querySelector(".chart-data").textContent); } catch (e) { return; }
    var P = data.primary;
    if (!P || !P.points || !P.points.length) return;
    box.classList.toggle("p-level", P.key === "level"); // gold is the battery's line alone
    var svg = box.querySelector(".chart-svg"), hov = box.querySelector(".chart-hov"), area = box.querySelector(".chart-area");
    var extra = null, W, H, L = 34, R = 44, T = 14, PH, LANE_Y, LANE_H = 10, PW, narrow, G = {};
    // a month of readings (Battery, the odometer) has no lane of states and no events to name
    var hasLane = (data.lane || []).length > 0 || (data.events || []).length > 0;
    // the margins fit the widest axis label, so "388mi" or "1,000V" is not cut off
    function widest(s) { return Math.max.apply(null, [s.lo, (s.lo + s.hi) / 2, s.hi].map(function (v, i) { return (Math.round(v) + (i === 2 ? s.unit.trim() : "")).length; })); }
    L = Math.max(34, Math.round(12 + 6.4 * widest(P)));
    R = Math.max(44, Math.round(12 + 6.4 * Math.max.apply(null, [0].concat((data.extras || []).map(widest)))));
    var end = Math.min(data.t1, Date.now() / 1000);

    function layout() {
      narrow = area.clientWidth < 520;
      W = narrow ? Math.max(280, Math.round(area.clientWidth)) : 680;
      H = narrow ? 230 : 250;
      PH = (narrow ? 150 : 170) + (hasLane ? 0 : 20);
      LANE_H = hasLane ? 10 : 0;
      LANE_Y = T + PH + (hasLane ? 10 : 0);
      PW = W - L - R;
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      area.classList.toggle("narrow", narrow);
    }
    function X(t) { return L + (t - data.t0) / (data.t1 - data.t0) * PW; }
    function Tx(x) { return data.t0 + (x - L) / PW * (data.t1 - data.t0); }
    function Y(s, v) { var c = Math.min(Math.max(v, s.lo), s.hi); return T + PH - (c - s.lo) / (s.hi - s.lo) * PH; }
    function el(n, a, p) { var e = document.createElementNS(NS, n); for (var k in a) e.setAttribute(k, a[k]); (p || svg).appendChild(e); return e; }
    function tx(x, y, s, cls, anchor, p) { var e = el("text", { x: x, y: y, "class": cls, "text-anchor": anchor || "start" }, p); e.textContent = s; return e; }
    function at(pts, t) {
      if (!pts.length) return null;
      if (t <= pts[0][0]) return pts[0][1];
      var lo = 0, hi = pts.length - 1;
      while (lo < hi) { var m = (lo + hi + 1) >> 1; if (pts[m][0] <= t) lo = m; else hi = m - 1; }
      return pts[lo][1];
    }
    function step(s) {
      var d = "", prev = null;
      s.points.forEach(function (p) {
        var x = X(p[0]).toFixed(1), y = Y(s, p[1]).toFixed(1);
        d += prev === null ? "M" + x + "," + y : "L" + x + "," + prev + "L" + x + "," + y;
        prev = y;
      });
      return d + "L" + X(end).toFixed(1) + "," + prev;
    }
    function fmt(s, v) { return v === null ? "—" : (Math.round(v * 10) / 10).toLocaleString() + s.unit; }
    function hm(sec) { var m = Math.round(sec / 60); if (m < 60) return m + " min"; var h = Math.floor(m / 60); m = m % 60; return h + " h" + (m ? " " + m + " min" : ""); }

    function render() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      var gid = box.id + "-fill", defs = el("defs", {}), grad = el("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
      el("stop", { offset: "0%", "class": "g0" }, grad);
      el("stop", { offset: "100%", "class": "g1" }, grad);
      [P.lo, (P.lo + P.hi) / 2, P.hi].forEach(function (v, i) {
        el("line", { x1: L, x2: L + PW, y1: Y(P, v), y2: Y(P, v), "class": "grid" });
        tx(L - 6, Y(P, v) + 3.5, Math.round(v) + (i === 2 ? P.unit.trim() : ""), "tk", "end");
      });
      // a label that would touch the one before it is left out ("Jul 8Jul 22" on a phone);
      // its grid line stays
      var lastRight = -Infinity;
      (data.ticks || []).forEach(function (k) {
        var x = X(k[0]), w = String(k[1]).length * 6.2, a = x - L < 14 ? "start" : (L + PW - x < 14 ? "end" : "middle");
        var left = a === "start" ? x : a === "end" ? x - w : x - w / 2;
        if (k[2]) el("line", { x1: x, x2: x, y1: T, y2: LANE_Y + LANE_H, "class": "day" });
        if (left < lastRight + 8) return;
        tx(x, H - 6, k[1], "tk", a);
        lastRight = left + w;
      });
      var line = step(P);
      el("path", { d: line + "L" + X(end) + "," + (T + PH) + "L" + X(P.points[0][0]) + "," + (T + PH) + "Z", fill: "url(#" + gid + ")" });
      el("path", { d: line, "class": "l-primary" });
      G.extra = el("g", {});  // after the fill, so the second line is not tinted by it
      if (hasLane) {
        el("rect", { x: L, y: LANE_Y, width: PW, height: LANE_H, rx: 3, "class": "lane-bg" });
        (data.lane || []).forEach(function (s) {
          el("rect", { x: X(s[0]), y: LANE_Y, width: Math.max(s[2] === "driving" ? 2 : 0.6, X(s[1]) - X(s[0])), height: LANE_H, "class": "ln-" + s[2] });
        });
      }
      G.cross = el("line", { y1: T, y2: LANE_Y + LANE_H, "class": "cross" });
      G.dot = el("circle", { r: 4.5, "class": "dotl" });
      G.dot2 = el("circle", { r: 3.5, "class": "dot2" });
      drawExtra();
    }
    function drawExtra() {
      while (G.extra.firstChild) G.extra.removeChild(G.extra.firstChild);
      G.dot2.style.display = extra ? "" : "none";
      if (!extra || !extra.points.length) return;
      el("path", { d: step(extra), "class": "l-extra" }, G.extra);
      [extra.lo, (extra.lo + extra.hi) / 2, extra.hi].forEach(function (v, i) {
        tx(L + PW + 6, Y(extra, v) + 3.5, Math.round(v) + (i === 2 ? extra.unit.trim() : ""), "tk t-extra", "start", G.extra);
      });
    }
    function eventAt(t) { var ev = data.events || []; for (var i = 0; i < ev.length; i++) if (t >= ev[i].a && t <= ev[i].b) return ev[i]; return null; }
    function laneAt(t) { var ln = data.lane || []; for (var i = 0; i < ln.length; i++) if (t >= ln[i][0] && t <= ln[i][1]) return ln[i]; return null; }
    function node(tag, cls, text, parent) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; if (parent) parent.appendChild(e); return e; }
    function card(t) {
      while (hov.firstChild) hov.removeChild(hov.firstChild);
      var ev = eventAt(t), ln = laneAt(t);
      var kind = ev ? (ev.kind === "drive" ? "driving" : "charging") : (ln ? ln[2] : "unknown");
      node("div", "hv-when", new Date(t * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }), hov);
      if (hasLane) {
        var st = node("div", "hv-state", null, hov);
        node("i", "k-" + kind, null, st);
        node("span", null, WORD[kind] + (ev && ev.title ? " · " + ev.title : ""), st);
      }
      var figs = node("div", "hv-figs", null, hov);
      [P].concat(data.extras || []).forEach(function (s) {
        var f = node("div", "hv-f", null, figs);
        node("b", s === P ? "c-primary" : "c-extra", fmt(s, at(s.points, t)), f);
        node("span", null, s.label, f);
      });
      var ctx = "";
      if (ev && ev.figs) ctx = ev.figs;
      else if (ln && !ev) {
        var a = at(P.points, ln[0]), b = at(P.points, Math.min(ln[1], end));
        ctx = WORD[kind] + " " + hm(Math.min(ln[1], end) - ln[0]) + (a !== null && b !== null && a - b >= 0.5 ? " · lost " + Math.round(a - b) + P.unit : "");
      }
      if (ctx) node("div", "hv-ctx", ctx, hov);
      if (narrow) { hov.style.left = ""; hov.style.top = ""; return; }
      var r = area.getBoundingClientRect(), px = X(t) / W * r.width, v = at(P.points, t), py = Y(P, v === null ? P.lo : v) / H * r.height;
      var cw = hov.offsetWidth || 260, ch = hov.offsetHeight || 120, left = px + 16, top = py - ch - 14;
      if (left + cw > r.width - 4) left = px - cw - 16;
      if (top < 0) top = py + 14;
      hov.style.left = Math.max(4, left) + "px";
      hov.style.top = Math.max(0, top) + "px";
    }
    function show(t) {
      var x = X(t), v = at(P.points, t);
      G.cross.setAttribute("x1", x);
      G.cross.setAttribute("x2", x);
      G.dot.setAttribute("cx", x);
      G.dot.setAttribute("cy", Y(P, v === null ? P.lo : v));
      if (extra) { var e = at(extra.points, t); G.dot2.setAttribute("cx", x); G.dot2.setAttribute("cy", Y(extra, e === null ? extra.lo : e)); }
      card(t);
    }
    function tFrom(ev) {
      var p = ev.touches ? ev.touches[0] : ev, r = svg.getBoundingClientRect();
      return Math.min(Tx(Math.min(Math.max((p.clientX - r.left) / r.width * W, L), L + PW)), end);
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
    function rest() { clearTimeout(linger); last = resting; show(resting); box.classList.add("resting"); }
    function move(ev) { clearTimeout(linger); box.classList.remove("resting"); last = tFrom(ev); show(last); }
    // A tap is followed by the browser's own mousemove; ignore it so the linger runs out.
    function touch(ev) { touched = Date.now(); move(ev); }
    rest();
    svg.addEventListener("mousemove", function (ev) { if (Date.now() - touched > 800) move(ev); });
    svg.addEventListener("touchstart", touch, { passive: true });
    svg.addEventListener("touchmove", touch, { passive: true });
    svg.addEventListener("touchend", function () { touched = Date.now(); clearTimeout(linger); linger = setTimeout(rest, 2500); }, { passive: true });
    svg.addEventListener("mouseleave", function () { rest(); });
    window.addEventListener("resize", function () { var was = narrow, w = W; layout(); if (was !== narrow || (narrow && w !== W)) render(); show(last); });
    box.querySelectorAll("[data-extra]").forEach(function (b) {
      b.addEventListener("click", function () {
        var key = b.getAttribute("data-extra");
        extra = null;
        (data.extras || []).forEach(function (x) { if (x.key === key) extra = x; });
        box.querySelectorAll("[data-extra]").forEach(function (o) { var s = o === b; o.classList.toggle("on", s); o.setAttribute("aria-checked", s ? "true" : "false"); });
        drawExtra();
        show(last);
      });
    });
  }

  function all() { document.querySelectorAll(".chartbox").forEach(draw); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all); else all();
})();

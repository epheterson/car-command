// The car's commands on Now (templates/_commands.html). State tiles and the quick row send on a tap; a tier 3 one
// (data-hairy) shows "Tap again" in itself first and sends on the second tap within four seconds. The panel's
// controls (data-send) send 1.5 s after the last tap on the same setting, so several taps are one command. Each
// POST carries the page's CSRF token; the result goes in the panel's status line and the card is redrawn from the
// reply. With a passkey for this site (meta cc-passkey), a tier 3 tap opens the phone's passkey sheet instead of
// asking again: its answer goes with that one command, and cancelling the sheet sends nothing. Without one, a tier 3
// command the server holds back opens the PIN or password field; once it is taken, the next tap sends.

// the pure parts, which the tests run in node
var CCCommands = {
  // a seat's next level: off, 1, 2, 3, off; a level not yet known starts at 1
  nextLevel: function (level) {
    var n = parseInt(level, 10);
    return isNaN(n) ? 1 : (n + 1) % 4;
  },
  // a stepper's next value inside its range, at the precision of its step
  step: function (value, dir, step, lo, hi) {
    var next = (parseFloat(value) || 0) + dir * step;
    next = Math.min(hi, Math.max(lo, next));
    return Math.round(next * 100) / 100;
  },
  // the cabin temperature is sent in °C; a page showing °F converts what it shows
  toCelsius: function (f) {
    return Math.round(((f - 32) * 5) / 9 * 10) / 10;
  },
  // the words a tile or button shows while it waits for its second tap
  confirmText: function (act) {
    return act.indexOf("Tap to ") === 0 ? "Tap again to " + act.slice(7) : act;
  },
  // the status line while a button waits: what the second tap does, from the tile's own words or the button's title
  againText: function (confirm, title) {
    if (confirm && confirm.indexOf("Tap again to ") === 0) return confirm;
    return title ? "Tap again to " + title.charAt(0).toLowerCase() + title.slice(1) : "Tap again to confirm";
  },
  // "Tap to unlock" -> "Unlock", for the status line
  nameOf: function (act) {
    var w = act.indexOf("Tap to ") === 0 ? act.slice(7) : act;
    return w.charAt(0).toUpperCase() + w.slice(1);
  },
  // All seats off: the seats this page has set above off; none known, just the two fronts' heat. Never all seven at once.
  // seats: [{ heat, vent, hl, vl, front }], the commands a seat has and the levels set here (undefined: not set here)
  seatsToClear: function (seats) {
    var out = [];
    seats.forEach(function (s) {
      if (s.heat && s.hl > 0) out.push({ key: s.heat, seat: s, mode: "heat" });
      if (s.vent && s.vl > 0) out.push({ key: s.vent, seat: s, mode: "vent" });
    });
    if (out.length) return out;
    seats.forEach(function (s) {
      if (s.front && s.heat) out.push({ key: s.heat, seat: s, mode: "heat" });
    });
    return out;
  },
  // one timer a setting: a new tap on the same group replaces what was waiting, and only the last is fired
  queue: function (delay, fire, setT, clearT) {
    var waiting = {};
    return {
      push: function (group, job) {
        if (waiting[group]) clearT(waiting[group].timer);
        waiting[group] = { job: job, timer: setT(function () { var j = waiting[group].job; delete waiting[group]; fire(j); }, delay) };
      },
      pending: function () { return Object.keys(waiting).length; },
    };
  },
};

if (typeof document !== "undefined") (function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";
  var CONFIRM_MS = 4000, SEND_MS = 1500;

  function dock(vin) {
    return document.querySelector('.cmd-more[data-cmd-vin="' + CSS.escape(vin) + '"]');
  }
  // the panel's status line: idle, wait (sending soon), ok, warn (tap again), err
  function say(vin, text, tone) {
    var box = dock(vin), line = box && box.querySelector("[data-cmd-said]");
    if (!line) return;
    line.querySelector("[data-said]").textContent = text;
    line.setAttribute("data-tone", tone || "idle");
  }
  function post(url, body, form) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: form ? { "X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded" } : { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
      body: form ? new URLSearchParams(body).toString() : JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, said: "The site didn't answer (" + r.status + ")." }; });
    });
  }
  function vinOf(el) {
    return el.closest("[data-cmd-vin]").getAttribute("data-cmd-vin");
  }

  // one command to the server; the reply says it in the status line and redraws the card
  function send(el, command, value, label, passkey) {
    var box = el.closest("[data-cmd-url]"), vin = box.getAttribute("data-cmd-vin");
    var tap = el.hasAttribute("data-cmd");  // a tile or quick button waits for the reply; a panel control stays live
    var line = tap && el.querySelector("[data-say]"), was = line && line.textContent;
    if (tap) {
      el.disabled = true;
      el.classList.add("busy");
      if (line) line.textContent = "Sending…";  // with a turning ring (style.css) until the reply; a car waking can take a while
    }
    say(vin, (label || "Sending") + "…", "wait");
    var body = { command: command, value: value };
    var where = /^(sched_charge$|depart)/.test(command) && box.querySelector("[data-where]");
    if (where) body.where = where.value;  // a schedule applies where the picker says; Off ignores it
    if (passkey) body.passkey = passkey;
    return post(box.getAttribute("data-cmd-url"), body)
      .then(function (out) {
        say(vin, out.said || (out.ok ? "Done." : "That didn't go through."), out.ok ? "ok" : "err");
        if (out.ok && out.card) {
          var old = document.querySelector('[data-live="' + CSS.escape(vin) + '"]');
          var t = document.createElement("template");
          t.innerHTML = out.card.trim();
          if (old && t.content.firstElementChild) { if (window.CCDockSync) window.CCDockSync(t.content); old.replaceWith(t.content.firstElementChild); }
        }
        if (out.step_up && !passkey) {
          var step = dock(vin) && dock(vin).querySelector("[data-step-up]");
          if (step) {
            step.hidden = false;
            step.querySelector("input").focus();
          }
        }
      })
      .catch(function () { say(vin, "The site didn't answer. Check the connection.", "err"); })
      .then(function () {
        if (!tap) return;
        el.disabled = false;
        el.classList.remove("busy");
        if (line && line.textContent === "Sending…") line.textContent = was;  // a redrawn card has its own; a quick button gets its name back
      });
  }

  // a tile or quick button waiting for its second tap shows it in itself, and goes back when it times out
  function disarm(btn) {
    if (btn._was == null) return;
    btn.querySelector("[data-say]").textContent = btn._was;
    btn._was = null;
    btn.classList.remove("confirm");
    clearTimeout(btn._timer);
  }
  function disarmAll() {
    document.querySelectorAll("[data-cmd].confirm").forEach(disarm);
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-cmd]");
    if (!btn || btn.disabled) return;
    e.preventDefault();
    var said = btn.querySelector("[data-say]");
    if (PK && btn.hasAttribute("data-hairy")) return confirmWithPasskey(btn, said.textContent);
    if (btn.hasAttribute("data-hairy") && btn._was == null) {
      disarmAll();
      btn._was = said.textContent;
      said.textContent = btn.getAttribute("data-confirm");
      btn.classList.add("confirm");
      say(vinOf(btn), CCCommands.againText(btn.getAttribute("data-confirm"), btn.getAttribute("title")), "warn");
      btn._timer = setTimeout(function () {
        disarm(btn);
        say(vinOf(btn), "Ready", "idle");
      }, CONFIRM_MS);
      return;
    }
    var act = btn._was != null ? btn._was : said.textContent;
    disarm(btn);
    send(btn, btn.getAttribute("data-cmd"), null, btn.getAttribute("data-name") || CCCommands.nameOf(act));
  });

  // a passkey for this site: one challenge fetched ahead, so a tap opens the sheet at once (Safari drops a sheet
  // asked for after a round trip), refreshed before the site's two minutes run out and after each use
  var pkMeta = document.querySelector('meta[name="cc-passkey"]');
  var PK = pkMeta && window.CCPasskey && window.PublicKeyCredential && window.isSecureContext && navigator.credentials ? pkMeta.getAttribute("content") : null;
  var pkReady = null;
  function pkFetch() {
    pkReady = null;
    return post(PK + "/challenge", { purpose: "cmd" }).then(function (o) { pkReady = o.ok ? o.options : null; }).catch(function () {});
  }
  if (PK) {
    pkFetch();
    setInterval(pkFetch, 50000);
  }
  function confirmWithPasskey(btn, act, command) {
    var vin = vinOf(btn), opts = pkReady;
    disarmAll();
    if (!opts) {
      say(vin, "One moment, then tap again", "warn");
      return pkFetch();
    }
    pkReady = null;
    say(vin, "Confirm with your passkey", "wait");
    navigator.credentials.get({ publicKey: CCPasskey.request(opts) })
      .then(function (cred) {
        return send(btn, command || btn.getAttribute("data-cmd"), null, btn.getAttribute("data-name") || CCCommands.nameOf(act), CCPasskey.answer(cred));
      })
      .catch(function (err) {
        say(vin, CCPasskey.quiet(err) ? "Ready" : "Not sent. Tap to retry.", CCPasskey.quiet(err) ? "idle" : "err");
      })
      .then(pkFetch);
  }

  // the panel's controls: each tap changes what is shown at once and (re)starts the setting's timer
  var queue = CCCommands.queue(SEND_MS, function (j) { send(j.el, j.command, j.value, "Sending"); }, setTimeout, clearTimeout);
  function later(el, group, command, value) {
    var vin = vinOf(el);
    say(vin, "Sending in a moment", "wait");
    queue.push(vin + "|" + group, { el: el, command: command, value: value });
  }
  function groupOf(el) {
    return el.getAttribute("data-group") || el.getAttribute("data-send");
  }

  // a stepper: shows the value in the unit the page shows, sends it in the unit the car takes
  function stepperValue(box, v) {
    box.setAttribute("data-value", v);
    box.querySelector("output").textContent = v + box.getAttribute("data-suffix");
  }
  function stepperSend(box) {
    var v = parseFloat(box.getAttribute("data-value"));
    later(box, groupOf(box), box.getAttribute("data-send"), box.getAttribute("data-conv") === "f" ? CCCommands.toCelsius(v) : v);
  }
  // a range's value shows beside it and on the card's own limit marker
  function rangeShow(range) {
    var v = range.value + (range.getAttribute("data-suffix") || "");
    var out = range.closest(".ctl-b");
    out = out && out.querySelector("output");
    if (out) out.textContent = v;
    range.setAttribute("aria-valuetext", v);
    var form = range.closest(".lim");
    if (form) {
      form.querySelector(".lim-v").textContent = v;
      form.style.setProperty("--lim", range.value);
    }
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    var go = t.closest("[data-send]");
    // a segmented option
    if (go && go.closest(".ctl-seg") && go.tagName === "BUTTON") {
      var seg = go.closest(".ctl-seg");
      seg.querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-pressed", b === go ? "true" : "false"); });
      // an option that asks for you (disarming the alarm): with a passkey, asked in this tap, which Safari insists on
      if (PK && go.hasAttribute("data-tier3")) return confirmWithPasskey(go, go.textContent, go.getAttribute("data-send"));
      var val = go.getAttribute("data-value");
      return later(go, groupOf(seg), go.getAttribute("data-send"), val === null ? null : Number(val));
    }
    var sw = t.closest(".ctl-sw");
    if (sw) {
      var on = sw.getAttribute("aria-checked") !== "true";
      sw.setAttribute("aria-checked", on ? "true" : "false");
      return later(sw, groupOf(sw), sw.getAttribute(on ? "data-send-on" : "data-send-off"), null);
    }
    var dir = t.closest("[data-stepper] [data-dir]");
    if (dir) {
      var box = dir.closest("[data-stepper]");
      stepperValue(box, CCCommands.step(box.getAttribute("data-value"), parseInt(dir.getAttribute("data-dir"), 10), parseFloat(box.getAttribute("data-step")), parseFloat(box.getAttribute("data-min")), parseFloat(box.getAttribute("data-max"))));
      return stepperSend(box);
    }
    var chip = t.closest("[data-chip]");
    if (chip) {
      var range = chip.closest(".ctl-b").querySelector("input[type=range]");
      range.value = chip.getAttribute("data-chip");
      rangeShow(range);
      return later(range, groupOf(range), range.getAttribute("data-send"), Number(range.value));
    }
    var tab = t.closest(".dock-tab");
    if (tab) return openTab(tab);
    var all = t.closest("[data-log-all]");
    if (all) {
      all.closest(".cmd-log").querySelectorAll("[data-more]").forEach(function (li) { li.hidden = false; });
      all.remove();
      return;
    }
    var seat = t.closest("[data-seat]");
    if (seat && !seat.disabled) return seatTap(seat);
    var mode = t.closest("[data-mode]");
    if (mode) {
      var panel = mode.closest(".ctl-seats");
      panel.querySelectorAll("[data-mode]").forEach(function (b) { b.setAttribute("aria-pressed", b === mode ? "true" : "false"); });
      return seatsDraw(panel);
    }
    var off = t.closest("[data-seats-off]");
    if (off) return seatsOff(off.closest(".ctl-seats"));
  });
  // the limit slider, the card's limit marker and the time field send when they settle
  document.addEventListener("input", function (e) {
    var range = e.target.closest("input[type=range][data-send]");
    if (!range) return;
    rangeShow(range);
    later(range, groupOf(range), range.getAttribute("data-send"), Number(range.value));
  });
  document.addEventListener("change", function (e) {
    var field = e.target.closest("input[data-minutes]");
    if (!field) return;
    var at = window.CCClock ? CCClock.parse(field.value) : null;
    later(field, groupOf(field), field.getAttribute("data-send"), at === null ? -1 : at);  // -1 is out of range: the site says so
  });
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.matches("form.cmd-arg")) return e.preventDefault();  // the limit marker sends on its own
    if (!form.matches("[data-step-up]")) return;
    e.preventDefault();
    var box = form.closest(".cmd-more");
    var vin = box.getAttribute("data-cmd-vin");
    var input = form.querySelector('[name="password"]');
    post(box.getAttribute("data-step-url"), { password: input.value }, true)
      .then(function (out) {
        input.value = "";
        say(vin, out.ok ? out.said + " Tap the command again." : out.said, out.ok ? "ok" : "err");
        if (out.ok) form.hidden = true;
      })
      .catch(function () { say(vin, "The site didn't answer. Check the connection.", "err"); });
  });

  // the seat map: one mode at a time; a seat's level is what was last tapped, since the car does not say
  function seatsMode(panel) {
    var on = panel.querySelector("[data-mode][aria-pressed=true]");
    return on ? on.getAttribute("data-mode") : "heat";
  }
  function seatsDraw(panel) {
    var mode = seatsMode(panel);
    panel.querySelectorAll("[data-seat]").forEach(function (s) {
      var has = !!s.getAttribute("data-" + mode), lv = s["_" + mode];
      s.disabled = !has;
      s.classList.toggle("vent", mode === "vent");
      var n = lv === undefined ? 0 : lv;
      s.querySelectorAll(".ctl-lv i").forEach(function (seg, i) { seg.classList.toggle("on", i < n); });
      s.classList.toggle("lit", n > 0);
      s.querySelector(".ctl-st").textContent = !has ? (mode === "vent" ? "No vent" : "No heat") : lv === undefined ? "—" : lv ? (mode === "vent" ? "Vent " : "Heat ") + lv : "Off";
      s.setAttribute("aria-label", s.getAttribute("data-name") + (mode === "vent" ? " vent " : " heat ") + (lv ? lv : lv === undefined ? "unknown" : "off"));
    });
  }
  function seatTap(seat) {
    var panel = seat.closest(".ctl-seats"), mode = seatsMode(panel);
    seat["_" + mode] = CCCommands.nextLevel(seat["_" + mode]);
    seatsDraw(panel);
    var key = seat.getAttribute("data-" + mode);
    later(seat, key, key, seat["_" + mode]);
  }
  function seatsOff(panel) {
    var seats = Array.prototype.map.call(panel.querySelectorAll("[data-seat]"), function (el) {
      return { el: el, heat: el.getAttribute("data-heat"), vent: el.getAttribute("data-vent"), hl: el._heat, vl: el._vent, front: el.classList.contains("front") };
    });
    CCCommands.seatsToClear(seats).forEach(function (j) {
      j.seat.el["_" + j.mode] = 0;
      later(j.seat.el, j.key, j.key, 0);
    });
    seatsDraw(panel);
  }
  document.querySelectorAll(".ctl-seats").forEach(seatsDraw);

  // the panel folds to its status line; the arrow above it opens it, and this browser remembers which
  var DOCK_KEY = "cc-dock-open";
  function dockOpen(btn, open) {
    var body = document.getElementById(btn.getAttribute("aria-controls"));
    if (!body) return;
    body.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? "Fewer controls" : "More controls");
    btn.title = open ? "Fewer controls" : "More controls";
  }
  var dockWas = null;
  try { dockWas = localStorage.getItem(DOCK_KEY); } catch (e) {}
  // the arrow lives in the card, which the stream and a command's reply draw again: a fresh one is set to what this browser chose
  window.CCDockSync = function (root) {
    (root || document).querySelectorAll("[data-dock-toggle]").forEach(function (b) {
      var open = dockWas === "1";
      b.setAttribute("aria-expanded", open ? "true" : "false");
      b.setAttribute("aria-label", open ? "Fewer controls" : "More controls");
      b.title = open ? "Fewer controls" : "More controls";
      var body = document.getElementById(b.getAttribute("aria-controls"));
      if (body) body.hidden = !open;
    });
  };
  window.CCDockSync();
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-dock-toggle]");
    if (!btn) return;
    var open = btn.getAttribute("aria-expanded") !== "true";
    dockWas = open ? "1" : "0";
    dockOpen(btn, open);
    try { localStorage.setItem(DOCK_KEY, open ? "1" : "0"); } catch (e2) {}
  });

  // the panel's tabs: one is always open; the log is drawn when its tab is first opened, never with the page
  function openTab(tab) {
    var panel = tab.closest(".dock-panel");
    panel.querySelectorAll(".dock-tab").forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    var pane = document.getElementById(tab.getAttribute("aria-controls"));
    if (!pane.matches("[data-cmd-log]") || pane._drawn) return;
    pane._drawn = true;
    fetch(pane.getAttribute("data-cmd-log"), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) { pane.querySelector(".cmd-log").innerHTML = html; })
      .catch(function () { pane._drawn = false; pane.querySelector(".cmd-log").textContent = "The log didn't load."; });
  }
  document.querySelectorAll('.dock-tab[aria-selected="true"]').forEach(function (t) {
    if (t.id.indexOf("tab-log-") === 0) openTab(t);
  });
  document.addEventListener("keydown", function (e) {
    var tab = e.target.closest && e.target.closest(".dock-tab");
    if (!tab || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
    var tabs = Array.prototype.slice.call(tab.parentNode.children), i = tabs.indexOf(tab) + (e.key === "ArrowRight" ? 1 : -1);
    var to = tabs[(i + tabs.length) % tabs.length];
    to.focus();
    e.preventDefault();
  });
})();

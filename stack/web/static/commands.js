// The car's commands on Now: the card's tiles and battery bar, and the dock under it (templates/_commands.html). A button asks for a second
// tap within four seconds; only then is the command POSTed, with the page's CSRF token. The
// result goes in the line under the card. A tier 3 command the server holds back for the
// password opens the password field; once it is taken, the next tap sends.
(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";
  var CONFIRM_MS = 4000;

  function said(vin) {
    var box = document.querySelector('.cmd-more[data-cmd-vin="' + CSS.escape(vin) + '"]');
    return box && box.querySelector("[data-cmd-said]");
  }
  function say(vin, text, bad) {
    var line = said(vin);
    if (!line) return;
    line.textContent = text;
    line.classList.toggle("err", !!bad);
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
  function argOf(btn) {
    var form = btn.closest("form.cmd-arg");
    if (!form) return null;
    var input = form.querySelector('[name="value"]');
    if (!input) return null;
    if (form.hasAttribute("data-minutes")) {
      var at = window.CCClock ? CCClock.parse(input.value) : null;
      return at === null ? -1 : at;  // -1 is out of range: the site says so
    }
    return input.value;
  }
  // a tile's icon and two lines go back as they were when its confirming tap times out
  function disarm(btn) {
    if (btn._was == null) return;
    btn.innerHTML = btn._was;
    btn._was = null;
    btn.classList.remove("confirm");
    clearTimeout(btn._cmdTimer);
  }
  function nameOf(btn) {
    return (btn.getAttribute("aria-label") || btn.textContent).trim();
  }
  function send(btn) {
    var box = btn.closest("[data-cmd-url]");
    var vin = box.getAttribute("data-cmd-vin");
    var label = nameOf(btn);
    btn.disabled = true;
    btn.classList.add("busy");
    say(vin, label + "…", false);
    post(box.getAttribute("data-cmd-url"), { command: btn.getAttribute("data-cmd"), value: argOf(btn) })
      .then(function (out) {
        // a switch tile shows what it did itself, so its success says nothing below the card
        var shown = out.ok && out.card && btn.classList.contains("tap");
        say(vin, shown ? "" : out.said || (out.ok ? "Done." : "That didn't go through."), !out.ok);
        // what the command set, on the card now rather than at the car's next report
        if (out.ok && out.card) {
          var old = document.querySelector('[data-live="' + CSS.escape(vin) + '"]');
          var t = document.createElement("template");
          t.innerHTML = out.card.trim();
          if (old && t.content.firstElementChild) {
            old.replaceWith(t.content.firstElementChild);
            return;
          }
        }
        if (out.step_up) {
          var step = document.querySelector('.cmd-more[data-cmd-vin="' + CSS.escape(vin) + '"] [data-step-up]');
          if (step) {
            step.hidden = false;
            step.querySelector("input").focus();
          }
        }
      })
      .catch(function () { say(vin, "The site didn't answer. Check the connection.", true); })
      .then(function () {
        btn.disabled = false;
        btn.classList.remove("busy");
      });
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-cmd]");
    if (!btn || btn.disabled) return;
    e.preventDefault();
    if (btn._was == null) {
      document.querySelectorAll("button[data-cmd].confirm").forEach(disarm);
      btn._was = btn.innerHTML;
      var words = btn.querySelector("[data-say]");
      if (words) words.textContent = "Sure?";
      else btn.textContent = "Tap to confirm";
      btn.classList.add("confirm");
      btn._cmdTimer = setTimeout(function () { disarm(btn); }, CONFIRM_MS);
      return;
    }
    disarm(btn);
    send(btn);
  });

  // the limit marker says its value as it moves; Set shows once it differs from the car's
  document.addEventListener("input", function (e) {
    var range = e.target.closest(".lim input[type=range]");
    if (!range) return;
    var form = range.closest(".lim"), set = form.querySelector("button[data-cmd]");
    form.querySelector(".lim-v").textContent = range.value + "%";
    range.setAttribute("aria-valuetext", range.value + "%");
    form.style.setProperty("--lim", range.value);
    disarm(set);
    set.hidden = range.value === form.getAttribute("data-current");
    set.textContent = "Set " + range.value + "%";
  });
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.matches("form.cmd-arg")) {
      e.preventDefault();
      var btn = form.querySelector("button[data-cmd]");
      if (btn) btn.click();
      return;
    }
    if (!form.matches("[data-step-up]")) return;
    e.preventDefault();
    var box = form.closest(".cmd-more");
    var vin = box.getAttribute("data-cmd-vin");
    var input = form.querySelector('[name="password"]');
    post(box.getAttribute("data-step-url"), { password: input.value }, true)
      .then(function (out) {
        input.value = "";
        say(vin, out.ok ? "Password taken for five minutes. Tap the command again." : out.said, !out.ok);
        if (out.ok) form.hidden = true;
      })
      .catch(function () { say(vin, "The site didn't answer. Check the connection.", true); });
  });

  // the log is drawn when it is opened, never with the page
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d.matches || !d.matches("details[data-cmd-log]") || !d.open) return;
    fetch(d.getAttribute("data-cmd-log"), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) { d.querySelector(".cmd-log").innerHTML = html; })
      .catch(function () { d.querySelector(".cmd-log").textContent = "The log didn't load."; });
  }, true);
})();

/* Settings: a change that raises power (commands on, a tier up, a token, the garage set to open) is confirmed first,
   with a passkey (Face ID) when there is one, else the PIN or the password, and then saved as it was posted. A change
   that lowers or keeps power posts at once. The server checks the same and draws the row itself when this did not run. */
(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content, root = document.querySelector('meta[name="cc-root"]')?.content || "";
  const post = (url, body, form) => fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": form ? "application/x-www-form-urlencoded" : "application/json" }, body: form ? new URLSearchParams(body).toString() : JSON.stringify(body || {}) })
    .then((r) => r.json().catch(() => ({ ok: false, said: "The site didn't answer (" + r.status + ")." })));
  const field = (f, name) => [...document.querySelectorAll(`[name="${name}"]`)].filter((e) => e.form === f);
  const was = (els) => { const e = els.find((x) => (x.type === "radio" ? x.defaultChecked : true)); return e ? (e.tagName === "SELECT" ? ([...e.options].find((o) => o.defaultSelected) || e.options[0] || {}).value : e.defaultValue) : undefined; };
  const now = (els) => { const e = els.find((x) => (x.type === "radio" ? x.checked : true)); return e ? e.value : undefined; };
  const changed = (f, name) => { const els = field(f, name); return els.length > 0 && now(els) !== was(els); };
  const RAISES = {
    always: () => true,
    commands: (f) => {
      const on = field(f, "commands_on")[0];
      if (on && on.checked && !on.defaultChecked) return true;
      const tiers = new Set([...f.elements].filter((e) => e.name && e.name.startsWith("tier_")).map((e) => e.name));
      return [...tiers].some((n) => Number(now(field(f, n)) || 1) > Number(was(field(f, n)) || 1));
    },
    switch: (f) => [...f.querySelectorAll("[data-power-switch]")].some((e) => e.checked && !e.defaultChecked),
    garage: (f) => now(field(f, "mode")) === "open" && (changed(f, "mode") || changed(f, "vin") || changed(f, "place")),
    door: (f) => f.dataset.garageOpen === "1" && changed(f, "entity"),
  };
  // the row drawn by the server (a refused save) reloads the page once confirmed; a cloned one posts its form
  const wire = (row, done) => {
    const said = row.querySelector("[data-cf-said]"), tell = (t) => { said.textContent = t; };
    row.querySelector("[data-cf-pk]")?.addEventListener("click", () => {
      if (!(window.CCPasskey && window.PublicKeyCredential)) return tell("This browser can't use a passkey here. Use the PIN or the password.");
      post(root + "/passkeys/challenge", { purpose: "step" })
        .then((o) => { if (!o.ok) throw new Error(o.said); return navigator.credentials.get({ publicKey: CCPasskey.request(o.options) }); })
        .then((cred) => post(root + "/passkeys/step", { passkey: CCPasskey.answer(cred) }))
        .then((out) => { if (!out.ok) throw new Error(out.said); done(); })
        .catch((err) => { if (!CCPasskey.quiet(err)) tell(err.message || "Passkey not recognised."); });
    });
    const secret = row.querySelector("[data-cf-secret]");
    const go = () => post(root + "/commands/step-up", { password: secret.value }, true).then((out) => { secret.value = ""; if (out.ok) done(); else tell(out.said); });
    row.querySelector("[data-cf-go]")?.addEventListener("click", go);
    secret?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    return row;
  };
  document.querySelectorAll("[data-cf-row]:not([hidden])").forEach((row) => wire(row, () => location.assign(location.pathname + location.search)));
  const blank = document.querySelector("template[data-cf-blank]");
  document.querySelectorAll("form[data-power]").forEach((f) => f.addEventListener("submit", (e) => {
    if (f.dataset.cfOk === "1" || !(RAISES[f.dataset.power] || (() => false))(f)) return;
    e.preventDefault();
    const send = () => { f.dataset.cfOk = "1"; f.classList.add("sb-saving"); HTMLFormElement.prototype.submit.call(f); };
    post(root + "/settings/confirmed").then((st) => {
      if (st.ok) return send();
      let row = f.querySelector("[data-cf-row]") || (blank && wire(blank.content.firstElementChild.cloneNode(true), send));
      if (!row) return;
      if (!row.isConnected) { const at = e.submitter && e.submitter.closest(".acts, .savebar, .set-c"); at ? at.before(row) : f.prepend(row); }
      row.hidden = false;
      row.scrollIntoView({ block: "nearest", behavior: "smooth" });
      row.querySelector("[data-cf-said]").textContent = st.said || "Confirm it's you to save this.";
      if (st.owner && st.ways && st.ways.passkey) row.querySelector("[data-cf-pk]")?.click(); // straight to Face ID: one tap, as a tier 3 command
      else row.querySelector("[data-cf-secret]")?.focus();
    });
  }));
})();

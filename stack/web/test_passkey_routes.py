"""The tier 3 step-up end to end: the command PIN, and passkeys made and used by a software authenticator through
the site's own routes. Nothing reaches a car: the commands go to the fake signing proxy of test_command_routes."""

import pytest

import auth
import passkeys
from test_command_routes import TESLA, client, csrf, site  # noqa: F401 - fixtures
from test_empty import bare  # noqa: F401 - fixtures
from test_passkeys import SoftKey

HTTPS = {"host": "garage.example.org", "x-forwarded-proto": "https"}
ORIGIN = "https://garage.example.org"


@pytest.fixture
def owner(site, client, monkeypatch):  # noqa: F811
    """Signed in with the password, the passkey table in memory."""
    app = site["app"]
    rows = []
    monkeypatch.setattr(passkeys, "listed", lambda conn, rp_id=None: [dict(r) for r in rows if not rp_id or r["rp_id"] == rp_id])
    monkeypatch.setattr(passkeys, "add", lambda conn, rp, key, name: rows.append({**key, "rp_id": rp.id, "name": name}))
    monkeypatch.setattr(passkeys, "remove", lambda conn, key_id: bool([rows.remove(r) for r in list(rows) if r["id"] == key_id]))
    monkeypatch.setattr(passkeys, "used", lambda conn, key_id, count: None)
    app.AUTH.update(enabled=True, password=auth.hash_password("correct horse", n=2**4), pin=None)
    app.PASSKEYS.clear()
    app.SETTING_STEPS.clear()
    cookie = auth.new_session(app.AUTH["secret"])
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    yield {**site, "rows": rows, "h": {"X-CSRF-Token": auth.csrf_token(app.AUTH["secret"], cookie)}}
    app.AUTH.update(enabled=False, password=None, pin=None)
    app.PASSKEYS.clear()
    app.FAILURES.clear(auth.throttle_key({}, "192.168.1.20"))


def step(client, owner, secret="correct horse"):  # noqa: F811
    return client.post("/commands/step-up", data={"password": secret}, headers=owner["h"])


def enrol(client, owner, soft):  # noqa: F811
    opts = client.post("/passkeys/challenge", json={"purpose": "add"}, headers={**owner["h"], **HTTPS}).json()["options"]
    answer = soft.create(passkeys.base64url_to_bytes(opts["challenge"]), origin=ORIGIN)
    return client.post("/settings/passkeys/add", json={"passkey": answer, "name": "Alpha's phone"}, headers={**owner["h"], **HTTPS})


def confirm(client, owner, soft, command="unlock", **kw):  # noqa: F811
    opts = client.post("/passkeys/challenge", json={"purpose": "cmd"}, headers={**owner["h"], **HTTPS}).json()["options"]
    answer = soft.get(passkeys.base64url_to_bytes(opts["challenge"]), origin=kw.pop("origin", ORIGIN), **kw)
    return answer, client.post(f"/cars/{TESLA}/command", json={"command": command, "passkey": answer}, headers={**owner["h"], **HTTPS})


def test_the_pin_is_set_after_a_step_up_and_then_confirms_commands(owner, client):  # noqa: F811
    app = owner["app"]
    assert client.post("/settings/pin", data={"pin": "482915", "confirm": "482915"}, headers=owner["h"]).status_code == 400, "a step-up first"
    assert step(client, owner).json()["ok"]
    for weak in ("111111", "123456", "987654", "12345"):
        assert client.post("/settings/pin", data={"pin": weak, "confirm": weak}, headers=owner["h"]).status_code == 400
    assert client.post("/settings/pin", data={"pin": "482915", "confirm": "482916"}, headers=owner["h"]).status_code == 400
    r = client.post("/settings/pin", data={"pin": "482915", "confirm": "482915"}, headers=owner["h"], follow_redirects=False)
    assert r.status_code == 303 and auth.verify_password(app.AUTH["pin"], "482915")
    assert any(k == "login" and t.startswith("A command PIN was set.") for k, t in owner["alerts"])
    app.STEP_UPS.clear()
    said = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=owner["h"]).json()["said"]
    assert said == "Enter your PIN or the password first."
    assert step(client, owner, "482915").json()["said"] == "PIN taken for two minutes."
    assert client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=owner["h"]).status_code == 200
    assert owner["log"][-1]["source"] == "site · PIN"


def test_a_wrong_pin_is_counted_and_announced(owner, client):  # noqa: F811
    owner["app"].AUTH["pin"] = auth.hash_password("482915", n=2**4)
    assert step(client, owner, "000000").status_code == 401
    assert any(t.startswith("A wrong PIN or password") for _, t in owner["alerts"])


def test_a_passkey_confirms_exactly_one_command(owner, client):  # noqa: F811
    soft = SoftKey()
    r = client.post("/passkeys/challenge", json={"purpose": "add"}, headers={**owner["h"], **HTTPS})
    assert r.status_code == 409 and r.json()["step_up"], "adding one needs a step-up once a password exists"
    step(client, owner)
    assert enrol(client, owner, soft).json()["ok"] and [r["name"] for r in owner["rows"]] == ["Alpha's phone"]
    assert any(t.startswith("A passkey was added to Car Command: Alpha's phone.") for _, t in owner["alerts"])
    owner["app"].STEP_UPS.clear()
    said = client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers={**owner["h"], **HTTPS}).json()["said"]
    assert said == "Confirm it with your passkey."
    answer, r = confirm(client, owner, soft)
    assert r.status_code == 200 and owner["proxy"].sent[-1][1].endswith("/command/door_unlock")
    assert owner["log"][-1]["source"] == "site · passkey"
    again = client.post(f"/cars/{TESLA}/command", json={"command": "unlock", "passkey": answer}, headers={**owner["h"], **HTTPS})
    assert again.status_code == 401 and again.json()["passkey_failed"], "an answer is good for one command"
    assert client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers={**owner["h"], **HTTPS}).status_code == 409, "and opens no window"


def test_a_passkey_answer_for_another_site_or_from_another_key_is_refused(owner, client):  # noqa: F811
    step(client, owner)
    soft = SoftKey()
    enrol(client, owner, soft)
    _, r = confirm(client, owner, soft, origin="https://garage.example.org.evil.example")
    assert r.status_code == 401
    _, r = confirm(client, owner, SoftKey())
    assert r.status_code == 401
    assert owner["proxy"].sent == [] and any(t.startswith("A passkey was refused for unlock.") for _, t in owner["alerts"])


def test_on_the_lan_address_there_are_no_passkeys_and_the_pin_or_password_still_works(owner, client):  # noqa: F811
    step(client, owner)
    enrol(client, owner, SoftKey())
    owner["app"].STEP_UPS.clear()
    lan = {**owner["h"], "host": "10.0.0.5:3044"}
    assert client.post("/passkeys/challenge", json={"purpose": "cmd"}, headers=lan).status_code == 409
    assert client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=lan).json()["said"] == "Enter the password first."
    assert client.post("/commands/step-up", data={"password": "correct horse"}, headers=lan).json()["ok"]
    assert client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers=lan).status_code == 200


def test_the_password_instead_of_a_passkey_is_announced(owner, client):  # noqa: F811
    step(client, owner)
    enrol(client, owner, SoftKey())
    owner["alerts"].clear()
    client.post("/commands/step-up", data={"password": "correct horse"}, headers={**owner["h"], **HTTPS})
    assert any(t.startswith("Commands were confirmed with the password instead of a passkey.") for _, t in owner["alerts"])


def test_removing_a_passkey_needs_a_step_up_and_is_announced(owner, client):  # noqa: F811
    step(client, owner)
    enrol(client, owner, SoftKey())
    key_id = owner["rows"][0]["id"]
    owner["app"].STEP_UPS.clear()
    assert client.post("/settings/passkeys/remove", data={"id": key_id}, headers=owner["h"]).status_code == 400
    assert owner["rows"]
    step(client, owner)
    assert client.post("/settings/passkeys/remove", data={"id": key_id}, headers=owner["h"], follow_redirects=False).status_code == 303
    assert owner["rows"] == [] and owner["app"].PASSKEYS == []
    assert any(t.startswith("A passkey was removed from Car Command: Alpha's phone.") for _, t in owner["alerts"])


def test_a_passkey_steps_up_settings_for_two_minutes(owner, client):  # noqa: F811
    step(client, owner)
    soft = SoftKey()
    enrol(client, owner, soft)
    owner["app"].STEP_UPS.clear()
    opts = client.post("/passkeys/challenge", json={"purpose": "step"}, headers={**owner["h"], **HTTPS}).json()["options"]
    answer = soft.get(passkeys.base64url_to_bytes(opts["challenge"]), origin=ORIGIN)
    assert client.post("/passkeys/step", json={"passkey": answer}, headers={**owner["h"], **HTTPS}).json()["ok"]
    assert client.post("/settings/pin", data={"pin": "482915", "confirm": "482915"}, headers=owner["h"], follow_redirects=False).status_code == 303
    assert client.post(f"/cars/{TESLA}/command", json={"command": "unlock"}, headers={**owner["h"], **HTTPS}).status_code == 409, "a Settings confirmation opens no command window"


def test_a_cmd_challenge_cannot_be_spent_on_settings(owner, client):  # noqa: F811
    step(client, owner)
    soft = SoftKey()
    enrol(client, owner, soft)
    owner["app"].STEP_UPS.clear()
    opts = client.post("/passkeys/challenge", json={"purpose": "cmd"}, headers={**owner["h"], **HTTPS}).json()["options"]
    answer = soft.get(passkeys.base64url_to_bytes(opts["challenge"]), origin=ORIGIN)
    assert client.post("/passkeys/step", json={"passkey": answer}, headers={**owner["h"], **HTTPS}).status_code == 401


def test_the_panel_asks_for_whatever_confirms_commands_here():
    import commands as cm
    from test_app_imports import _env

    cmd = {"reason": None, "allowed": {"lock"}, "more": [cm.CATALOGUE["unlock"]]}
    t = _env().from_string('{% import "_commands.html" as c %}{{ c.dock(cmd, car, root, none, step) }}')
    label = lambda **w: t.render(cmd=cmd, car={"vin": "V1", "make": "Tesla"}, root="", step={"passkey": False, "here": True, "pin": False, "password": False, **w})  # noqa: E731
    assert "PIN, for two minutes" in label(pin=True) and 'inputmode="numeric"' in label(pin=True)
    assert "Password, for two minutes" in label(password=True) and 'autocomplete="current-password"' in label(password=True)
    assert "PIN or password, for two minutes" in label(pin=True, password=True)
    assert "data-step-up" not in label(), "nothing to type: the refusal says to add a passkey or a PIN"


def test_the_page_says_a_passkey_is_here_only_when_one_is(owner, client):  # noqa: F811
    step(client, owner)
    enrol(client, owner, SoftKey())
    page = lambda h: client.get("/settings/access", headers=h).text  # noqa: E731
    assert '<meta name="cc-passkey" content="/passkeys">' in page(HTTPS)
    assert "cc-passkey" not in page({"host": "10.0.0.5:3044"}), "the LAN address cannot use it"
    assert "cc-passkey" not in page({"host": "other.example.org", "x-forwarded-proto": "https"}), "a passkey belongs to one name"


def test_commands_js_turns_passkey_json_both_ways():
    """The WebAuthn plumbing in static/passkeys.js, run in node: base64url both ways, the site's options as the
    browser takes them, the phone's answer as the site reads it, and a closed sheet counted as no failure."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = (Path(__file__).parent / "static" / "passkeys.js").read_text() + """
var C = CCPasskey, bytes = function (b) { return Array.from(new Uint8Array(b)); };
var req = C.request({ challenge: "AAEC_-8", rpId: "garage.example.org", timeout: 60000, userVerification: "required", allowCredentials: [{ id: "Y3JlZA", type: "public-key", transports: ["internal"] }] });
var buf = function (a) { return new Uint8Array(a).buffer; };
var ans = C.answer({ id: "Y3JlZA", rawId: buf([99, 114, 101, 100]), type: "public-key", response: { clientDataJSON: buf([123, 125]), authenticatorData: buf([1, 2, 255]), signature: buf([251, 255]), userHandle: null } });
console.log(JSON.stringify({
  round: C.toB64u(C.fromB64u("AAEC_-8")), challenge: bytes(req.challenge), rp: req.rpId, uv: req.userVerification,
  ids: req.allowCredentials.map(function (c) { return bytes(c.id); }), ans: ans,
  made: C.made({ id: "Y3JlZA", rawId: buf([99, 114, 101, 100]), type: "public-key", response: { clientDataJSON: buf([123, 125]), attestationObject: buf([161]), getTransports: function () { return ["internal", "hybrid"]; } } }),
  make: (function (o) { return { user: bytes(o.user.id), name: o.user.name, ex: o.excludeCredentials.map(function (c) { return bytes(c.id); }), sel: o.authenticatorSelection }; })(C.creation({ challenge: "AA", rp: { id: "garage.example.org", name: "Car Command" }, user: { id: "AQI", name: "Car Command", displayName: "Car Command" }, pubKeyCredParams: [], authenticatorSelection: { userVerification: "required" }, excludeCredentials: [{ id: "Y3JlZA", type: "public-key" }] })),
  quiet: [C.quiet({ name: "NotAllowedError" }), C.quiet({ name: "AbortError" }), C.quiet({ name: "SecurityError" }), C.quiet(null)],
}));
"""
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert got["round"] == "AAEC_-8" and got["challenge"] == [0, 1, 2, 255, 239] and got["rp"] == "garage.example.org" and got["uv"] == "required"
    assert got["ids"] == [[99, 114, 101, 100]]
    assert got["ans"] == {"id": "Y3JlZA", "rawId": "Y3JlZA", "type": "public-key", "response": {"clientDataJSON": "e30", "authenticatorData": "AQL_", "signature": "-_8", "userHandle": None}}
    assert got["quiet"] == [True, True, False, False]
    assert got["made"] == {"id": "Y3JlZA", "rawId": "Y3JlZA", "type": "public-key", "response": {"clientDataJSON": "e30", "attestationObject": "oQ", "transports": ["internal", "hybrid"]}}
    assert got["make"] == {"user": [1, 2], "name": "Car Command", "ex": [[99, 114, 101, 100]], "sel": {"userVerification": "required"}}

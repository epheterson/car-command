"""Alert channels added in Settings: checked, sealed, routed like .env's, and sent a test."""

import json
import pathlib

import pytest

import alertchans
import sources as sources_mod
from recorder import Recorder
from test_empty import EmptyConnection, bare, client  # noqa: F401 - fixtures
from test_settings_routes import form


class Store(EmptyConnection):
    """cc_state as rows: the statements alertchans runs."""

    def __init__(self):
        self.rows = {}

    def execute(self, sql, args=()):
        text = " ".join(sql.split())
        if text.startswith("INSERT"):
            self.rows[args[0]] = args[1]
        elif text.startswith("DELETE"):
            self.rows.pop(args[0], None)
        elif text.startswith("SELECT key, value") and args:
            rows = [{"key": k, "value": v} for k, v in self.rows.items() if k.startswith(args[0][:-1])]
            return type("R", (), {"fetchall": lambda s: rows})()
        return super().execute(sql, args)


DISCORD = {"discord_url": "https://discord.com/api/webhooks/1/abcdWXYZ"}
TELEGRAM = {"telegram_token": "123:SECRETtok", "telegram_chat_id": "42"}


def test_a_form_is_trimmed_and_checked_for_its_kind():
    assert alertchans.check("discord", {"discord_url": "  https://discord.com/api/webhooks/1/x  "}) == {"url": "https://discord.com/api/webhooks/1/x"}
    assert alertchans.check("webhook", {"webhook_url": "http://hub.local/alert"}) == {"url": "http://hub.local/alert", "token": ""}
    for kind, bad in (
        ("discord", {"discord_url": "http://discord.com/x"}),
        ("discord", {}),
        ("telegram", {"telegram_token": "t"}),
        ("email", {"email_smtp_url": "imap://h", "email_to": "a@b.c"}),
        ("email", {"email_smtp_url": "smtp://h", "email_to": "nobody"}),
        ("webhook", {"webhook_url": "ftp://h"}),
        ("sms", {}),
    ):
        with pytest.raises(alertchans.ChannelError):
            alertchans.check(kind, bad)


def test_a_channel_is_sealed_shows_its_last_four_and_opens_again():
    conn, vault = Store(), sources_mod.Vault(sources_mod.new_key())
    conf = alertchans.check("telegram", TELEGRAM)
    alertchans.save(conn, vault, "telegram", conf)
    stored = conn.rows["alert_channel:telegram"]
    assert "SECRETtok" not in stored and "123:" not in stored, "never stored plain"
    assert alertchans.hints(conn) == {"telegram": json.loads(stored)["last4"]}
    assert alertchans.load(conn, vault) == {"telegram": conf}
    assert alertchans.load(conn, sources_mod.Vault(sources_mod.new_key())) == {}, "another key opens nothing"
    alertchans.forget(conn, "telegram")
    assert alertchans.load(conn, vault) == {} and alertchans.hints(conn) == {}


def test_a_blob_sealed_for_one_kind_does_not_open_as_another():
    conn, vault = Store(), sources_mod.Vault(sources_mod.new_key())
    alertchans.save(conn, vault, "discord", alertchans.check("discord", DISCORD))
    conn.rows["alert_channel:webhook"] = conn.rows.pop("alert_channel:discord")
    assert alertchans.load(conn, vault) == {}


def _recorder(monkeypatch, sent, **env):
    for k in ("DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_URL", "EMAIL_TO", "PUSHOVER_TOKEN", "PUSHOVER_USER", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import recorder as rec

    monkeypatch.setattr(rec, "send_discord", lambda url, text: sent.append(("discord", url, text)) or True)
    monkeypatch.setattr(rec, "send_telegram", lambda tok, chat, text: sent.append(("telegram", tok, chat)) or True)
    r = Recorder(lambda: {}, lambda: EmptyConnection(), log_connect=None)
    r.log_connect = None
    return r


def test_stored_and_env_channels_go_down_the_same_road(monkeypatch):
    sent = []
    r = _recorder(monkeypatch, sent, DISCORD_WEBHOOK_URL="https://env.example/hook")
    r.stored = {"telegram": {"token": "tok", "chat_id": "7"}}
    assert r.channels() == ["discord", "telegram"]
    r.notify("hello", "charge_start")
    assert sent == [("discord", "https://env.example/hook", "hello"), ("telegram", "tok", "7")]


def test_a_channel_from_env_wins_over_a_stored_one_of_its_kind(monkeypatch):
    sent = []
    r = _recorder(monkeypatch, sent, DISCORD_WEBHOOK_URL="https://env.example/hook")
    r.stored = {"discord": {"url": "https://stored.example/hook"}}
    r.notify("hi", "charge_start")
    assert sent == [("discord", "https://env.example/hook", "hi")]
    assert r.env_confs().keys() == {"discord"}


def test_a_stored_channel_obeys_the_routes_and_master_switch(monkeypatch):
    sent = []
    r = _recorder(monkeypatch, sent)
    r.stored = {"discord": {"url": "https://stored.example/hook"}, "telegram": {"token": "t", "chat_id": "1"}}
    r.routes = {"parked": {"telegram"}, "driving": set()}
    r.notify("a", "parked")
    r.notify("b", "driving")
    assert [s[0] for s in sent] == ["telegram"]


def test_a_test_send_uses_the_fields_given_and_saves_nothing(monkeypatch):
    sent = []
    r = _recorder(monkeypatch, sent)
    assert r.send("discord", "test", "test", {"url": "https://typed.example/hook"}) is True
    assert sent == [("discord", "https://typed.example/hook", "test")] and r.channels() == []


@pytest.fixture
def chans(bare, monkeypatch):  # noqa: F811
    store = Store()
    monkeypatch.setattr(bare, "db", lambda: store)
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(sources_mod.new_key()))
    monkeypatch.setattr(bare.recorder, "stored", {})
    for k in ("DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_URL", "EMAIL_TO", "PUSHOVER_TOKEN", "PUSHOVER_USER", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bare.recorder, "webhook_url", None)
    monkeypatch.setattr(bare.recorder, "telegram", (None, None))
    monkeypatch.setattr(bare.recorder, "email", (None, None, None))
    monkeypatch.setattr(bare.recorder, "pushover", (None, None))
    monkeypatch.setattr(bare.recorder, "hook", (None, None))
    return store


def test_the_page_offers_every_kind_as_a_form_when_none_is_set_up(chans, client):  # noqa: F811
    page = client.get("/settings/alerts").text
    for kind in ("discord", "telegram", "email", "pushover", "webhook"):
        assert f'action="/settings/alerts/channel/{kind}/add"' in page, kind
    assert "No channel set up" in page


def test_an_address_or_token_box_takes_the_whole_row_under_its_label(chans, client):  # noqa: F811
    """A Discord webhook is about 120 characters: the box is the card's width, label above, on a desktop as on a phone."""
    from pathlib import Path

    page = client.get("/settings/alerts").text
    assert page.count('class="set-row under full"') == page.count('class="field chan-f wide"') >= 7
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    assert ".set-row.under.full > .set-l { flex:1 0 100%; }" in css and ".set-row.under.full .set-c .field { flex:1 1 auto; width:100%; }" in css
    assert "width:min(26em, 60vw)" not in css


def test_adding_a_channel_seals_it_routes_it_and_lists_it_with_test_and_remove(bare, chans, client, monkeypatch):  # noqa: F811
    r = client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD))
    assert r.status_code == 303
    assert "abcdWXYZ" not in chans.rows["alert_channel:discord"]
    assert bare.recorder.channels() == ["discord"]
    page = client.get("/settings/alerts").text
    assert 'id="chan-discord"' in page and 'form="chan-test-discord"' in page and 'form="chan-rm-discord"' in page and "added here" in page
    assert 'action="/settings/alerts/channel/discord/add"' not in page, "a kind that is set up is not offered again"
    r = client.post("/settings/alerts/channel/discord/remove", data=form(bare, {}))
    assert r.status_code == 303 and bare.recorder.channels() == [] and not any(k.startswith('alert_channel') for k in chans.rows)


def test_send_test_on_a_saved_channel_posts_to_its_own_form_not_the_page(bare, chans, client):  # noqa: F811
    """A button owned by a form="..." has no formaction attribute, and the `formAction` property then reads as the page's own URL:
    the test went to the page and nothing was sent. The script reads the attribute and falls back to the owner form's action."""
    client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD))
    page = client.get("/settings/alerts").text
    assert '<form id="chan-test-discord" method="post" action="/settings/alerts/channel/discord/test"' in page
    assert 'btn.getAttribute("formaction") || btn.form.action' in page
    assert "btn.formAction" not in page


def test_no_script_reads_the_formaction_property():
    """`.formAction` falls back to the document URL for a button that names its form with form="..."; read the attribute."""
    root = pathlib.Path(__file__).parent
    for path in [*(root / "templates").glob("*.html"), *(root / "static").glob("*.js")]:
        assert ".formAction" not in path.read_text(), path.name


def test_a_bad_form_is_refused_and_saves_nothing(bare, chans, client):  # noqa: F811
    r = client.post("/settings/alerts/channel/discord/add", data=form(bare, {"discord_url": "http://x"}))
    assert r.status_code == 400 and "https address" in r.text and not any(k.startswith("alert_channel") for k in chans.rows)


def test_a_test_send_reports_what_happened(bare, chans, client, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(bare.recorder, "send", lambda ch, text, kind, conf=None: seen.append((ch, conf)) or True)
    r = client.post("/settings/alerts/channel/telegram/test", data=form(bare, TELEGRAM))
    assert r.status_code == 200 and "Sent." in r.text and seen == [("telegram", {"token": "123:SECRETtok", "chat_id": "42"})]
    assert not any(k.startswith("alert_channel") for k in chans.rows), "a test saves nothing"
    monkeypatch.setattr(bare.recorder, "send", lambda *a, **k: False)
    r = client.post("/settings/alerts/channel/telegram/test", data=form(bare, TELEGRAM))
    assert r.status_code == 502 and "Nothing arrived" in r.text
    assert client.post("/settings/alerts/channel/discord/test", data=form(bare, {})).status_code == 400, "nothing typed, nothing set up"


def test_a_channel_from_env_says_so_and_cannot_be_removed_here(bare, chans, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare.recorder, "webhook_url", "https://env.example/hook")
    page = client.get("/settings/alerts").text
    assert "from .env" in page and 'form="chan-rm-discord"' not in page and 'form="chan-test-discord"' in page
    assert client.post("/settings/alerts/channel/discord/remove", data=form(bare, {})).status_code == 409
    assert client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD)).status_code == 409


def test_without_a_secret_key_nothing_is_added(bare, chans, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "VAULT", sources_mod.Vault(None))
    r = client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD))
    assert r.status_code == 400 and "CC_SECRET_KEY" in r.text and not any(k.startswith("alert_channel") for k in chans.rows)


def test_a_new_channel_starts_with_the_short_default_set_and_leaves_the_others_alone():
    import notify

    first = notify.routes_with_channel({}, [], "discord")
    assert set(notify.KINDS) == set(first)
    assert {k for k, v in first.items() if v} == set(notify.DEFAULT_KINDS) and len(notify.DEFAULT_KINDS) < len(notify.KINDS) // 2
    assert {"charge_done", "alarm", "connector"} <= set(notify.DEFAULT_KINDS) and "parked" not in notify.DEFAULT_KINDS
    # a channel already there kept every alert it had, switched off ones included, and the new one gets only the defaults
    both = notify.routes_with_channel({"parked": set(), "door": {"telegram"}}, ["telegram"], "discord")
    assert both["parked"] == [] and both["door"] == ["telegram"] and both["driving"] == ["telegram"]
    assert both["charge_done"] == ["discord", "telegram"]


def test_a_channel_set_in_env_with_no_chosen_routing_starts_with_the_same_short_set():
    import notify

    # nothing ever saved: the .env channel gets the defaults, and is written down as chosen
    routes, routed = notify.settle_routes({"alert_routes": None, "alert_routed": None, "muted_alerts": []}, ["discord"])
    assert {k for k, v in routes.items() if v} == set(notify.DEFAULT_KINDS) and routed == ["discord"]
    # an app channel already routed, then one set in .env: the old one is untouched, the new one takes only the defaults
    st = {"alert_routes": {k: ["telegram"] for k in notify.KINDS}, "alert_routed": ["telegram"]}
    routes, routed = notify.settle_routes(st, ["discord", "telegram"])
    assert routes["parked"] == ["telegram"] and routes["charge_done"] == ["discord", "telegram"] and routed == ["discord", "telegram"]
    # once chosen, never again: a channel with every alert switched off stays off
    chosen = {"alert_routes": {k: [] for k in notify.KINDS}, "alert_routed": ["discord"]}
    assert notify.settle_routes(chosen, ["discord"]) is None


def test_an_install_from_before_the_marker_keeps_the_routing_it_has_and_writes_the_seed_down():
    import notify

    # saved routing with no marker: every channel it has was chosen, whatever the routing says (here Discord is on nothing)
    saved = {"alert_routes": {"parked": ["webhook"], "door": []}, "alert_routed": None}
    routes, routed = notify.settle_routes(saved, ["discord", "webhook"])
    assert routes is None and routed == ["discord", "webhook"], "routing left exactly as saved; only the marker is written"
    # the next channel set in .env is then new, not old
    again = {**saved, "alert_routed": routed}
    routes, routed = notify.settle_routes(again, ["discord", "pushover", "webhook"])
    assert (
        routes["parked"] == ["webhook"] and routes["charge_done"] == ["discord", "pushover", "webhook"] and routes["door"] == []
    ), "a kind never set went to every channel; it still does, plus the new one"
    legacy = {"alert_routes": None, "muted_alerts": ["parked"], "alert_routed": None}
    assert notify.settle_routes(legacy, ["discord"]) == (None, ["discord"]), "the old muted list is routing too"


def test_start_writes_the_default_set_for_an_env_channel_and_never_rewrites_chosen_routing(bare, chans, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare.recorder, "webhook_url", "https://env.example/hook")
    bare._cache.pop("settings", None)
    bare.settle_channel_routes()
    saved = json.loads(chans.rows["alert_routes"])
    assert saved["charge_done"] == ["discord"] and saved["parked"] == [] and json.loads(chans.rows["alert_routed"]) == ["discord"]
    before = dict(chans.rows)
    bare._cache.pop("settings", None)
    bare.settle_channel_routes()
    assert chans.rows == before, "the second start finds it chosen and writes nothing"


def test_adding_a_channel_writes_its_routes(bare, chans, client):  # noqa: F811
    client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD))
    saved = json.loads(chans.rows["alert_routes"])
    assert saved["charge_done"] == ["discord"] and saved["parked"] == [] and saved["door"] == []


def test_a_test_send_answers_json_for_the_page_beside_its_button(bare, chans, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare.recorder, "send", lambda *a, **k: False)
    r = client.post("/settings/alerts/channel/telegram/test", data=form(bare, TELEGRAM), headers={"accept": "application/json"})
    assert r.status_code == 502 and r.json() == {"ok": False, "text": "Nothing arrived: check the address and keys."}
    r = client.post("/settings/alerts/channel/discord/test", data=form(bare, {}), headers={"accept": "application/json"})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_a_test_send_without_the_script_keeps_what_was_typed_and_opens_the_form(bare, chans, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare.recorder, "send", lambda *a, **k: True)
    page = client.post("/settings/alerts/channel/telegram/test", data=form(bare, TELEGRAM)).text
    assert 'value="123:SECRETtok"' in page and 'value="42"' in page
    assert 'id="chan-add-telegram" open' in page.replace('\n', ' ') or "chan-add-telegram\" open" in page
    assert "Sent." in page


def test_the_page_lists_channels_above_the_add_form_and_switches_save_themselves(bare, chans, client):  # noqa: F811
    page = client.get("/settings/alerts").text
    assert page.index("No channel set up") < page.index('id="add-channel"')
    client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD))
    page = client.get("/settings/alerts").text
    assert 'data-autosave' in page and 'data-saved' in page and 'data-res="discord"' in page and 'data-test-chan="discord"' in page


def test_a_switch_saved_in_the_background_answers_json(bare, chans, client):  # noqa: F811
    client.post("/settings/alerts/channel/discord/add", data=form(bare, DISCORD))
    r = client.post("/settings/alerts", data=form(bare, {"routes_shown": "1", "routes_kinds": "parked", "route_parked_discord": "on"}), headers={"accept": "application/json"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert json.loads(chans.rows["alert_routes"])["parked"] == ["discord"]

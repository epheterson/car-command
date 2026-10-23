"""Fleet API use: every call counted by kind and car, the month rolling over, the budget's
steps, a command always going, and the 80% alert sent once. Nothing reaches Tesla."""

from datetime import datetime, timezone

import pytest

import fleet_usage as fu
import tesla_cmd
import tesla_fleet
from test_command_routes import TESLA, csrf, site  # noqa: F401 - fixtures
from test_empty import EmptyConnection, bare, client  # noqa: F401 - fixtures

VIN = "5YJ3E1EA0SF000001"
BASE = "https://fleet.example.test"


def at(y, m, d=5):
    return datetime(y, m, d, 12, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def meter(monkeypatch):
    m = fu.Meter(clock=Clock(at(2026, 10)))
    monkeypatch.setattr(fu, "METER", m)
    return m


class Conn:
    def __init__(self, usage=(), calls=(), alerts=()):
        self.sql, self.sets = [], {"usage": list(usage), "calls": list(calls), "alerts": list(alerts)}

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        text = sql
        rows = self.sets["usage"] if "FROM cc_fleet_usage" in text else self.sets["calls"] if "FROM cc_fleet_calls" in text else self.sets["alerts"] if "FROM cc_fleet_alerts" in text else []

        class R:
            def fetchall(_):
                return rows

        return R()

    def commit(self):
        pass


def answers(*rows):
    queue = list(rows)

    def call(method, url, **kw):
        return queue.pop(0) if queue else (200, {"response": {}})

    return call


def test_classify_names_each_kind_of_call():
    c = fu.classify
    assert c("POST", f"{BASE}/api/1/vehicles/{VIN}/wake_up") == ("wakes", "wake_up", VIN)
    assert c("POST", f"https://p:4443/api/1/vehicles/{VIN}/command/charge_stop") == ("commands", "charge_stop", VIN)
    assert c("GET", f"{BASE}/api/1/vehicles/{VIN}/vehicle_data?endpoints=x") == ("data", "vehicle_data", VIN)
    assert c("GET", f"{BASE}/api/1/vehicles/{VIN}") == ("data", "vehicle", VIN)
    assert c("POST", f"{BASE}/api/1/vehicles/fleet_telemetry_config_jws", {"vins": [VIN]}) == ("other", "fleet_telemetry_config_jws", VIN)
    assert c("GET", f"{BASE}/api/1/vehicles/{VIN}/fleet_telemetry_config")[0] == "other"
    assert c("GET", f"{BASE}/api/1/dx/charging/history?vin={VIN}")[:2] == ("data", "charging_history")
    assert c("GET", f"{BASE}/api/1/dx/charging/invoice/abc")[:2] == ("data", "charging_invoice")
    assert c("GET", f"{BASE}/api/1/energy_sites/123/live_status")[:2] == ("data", "energy_live_status")
    assert c("POST", tesla_fleet.TOKEN)[0] == "other"
    assert c("GET", f"{BASE}/api/1/vehicles")[0] == "other"
    assert c("POST", f"{BASE}/api/1/vehicles/fleet_status")[0] == "other"


def test_fleet_counts_every_call_by_kind(meter):
    f = tesla_fleet.Fleet(
        "id",
        "secret",
        call=answers((200, {"response": []}), (200, {"response": {}}), (200, {"response": {"updated_vehicles": 1}}), (200, {"response": {}}), (200, {"response": {"config": None}})),
        raw=lambda *a, **k: (200, "application/pdf", b"%PDF-x"),
    )
    f.vehicles("t", BASE)
    f.vehicle_config("t", BASE, VIN)
    f.telemetry_create("t", BASE, [VIN], "jws")
    f.charging_history("t", BASE, VIN)
    f.telemetry_config("t", BASE, VIN)
    f.charging_invoice("t", BASE, "abc")
    assert meter.calls() == {"data": 3, "commands": 0, "wakes": 0, "signals": 0, "other": 3}
    assert meter.calls(vin=VIN)["other"] == 2
    assert [r[3] for r in list(meter.recent)[:2]] == ["charging_invoice", "fleet_telemetry_config"]


def test_the_signing_proxy_counts_commands_wakes_and_the_wake_poll(meter):
    ok = (200, {"response": {"result": True}})
    p = tesla_cmd.Proxy("https://p:4443", call=answers((408, {"error": "vehicle unavailable"}), (200, {"response": {"state": "online"}}), ok), sleep=lambda s: None)
    assert p.send("t", VIN, "charge_stop").ok
    # first try, the wake, then the command again
    assert meter.calls() == {"data": 0, "commands": 2, "wakes": 1, "signals": 0, "other": 0}
    p2 = tesla_cmd.Proxy("https://p:4443", call=answers((200, {"response": {"state": "asleep"}}), (200, {"response": {"state": "online"}})), sleep=lambda s: None)
    assert p2.send("t", VIN, "wake").woke
    assert meter.calls()["wakes"] == 2 and meter.calls()["data"] == 1


def test_an_attempt_is_logged_but_only_what_tesla_bills_is_counted(meter):
    import httpx

    def down(*a, **k):
        raise httpx.ConnectError("no")

    with pytest.raises(tesla_cmd.Unavailable):
        tesla_cmd.Proxy("https://p:4443", call=down)._post("t", f"/api/1/vehicles/{VIN}/command/honk_horn", None)
    assert list(meter.recent)[0][4] == 0 and meter.calls()["commands"] == 0
    for status in (500, 503):
        meter.count("data", "vehicle_data", VIN, status)
    assert meter.calls()["data"] == 0 and len(meter.recent) == 3
    for status in (200, 408, 429, 499):
        meter.count("data", "vehicle_data", VIN, status)
    assert meter.calls()["data"] == 4 and meter.spend() == pytest.approx(0.008)
    conn = Conn()
    meter.tick(conn)
    assert [p for q, p in conn.sql if "INTO cc_fleet_usage" in q] == [("2026-10", VIN, "data", 4)]
    assert sum(1 for q, _ in conn.sql if "INTO cc_fleet_calls" in q) == 7


def test_the_month_rolls_over(meter):
    meter.count("data", "vehicle_data", VIN, 200)
    meter.count("wakes", "wake_up", VIN, 200)
    assert meter.spend() == pytest.approx(0.022)
    meter.clock.t = at(2026, 11, 1)
    assert meter.calls() == dict.fromkeys(fu.CATEGORIES, 0) and meter.spend() == 0 and meter.step() == "ok"
    assert meter.calls("2026-10")["wakes"] == 1
    meter.count("data", "vehicle_data", VIN, 200)
    assert meter.calls()["data"] == 1 and meter.calls("2026-10")["data"] == 1
    assert fu.month_back("2026-02", 3) == "2025-11"


def fill(meter, spend_wakes):
    for _ in range(spend_wakes):
        meter.count("wakes", "wake_up", VIN, 200)  # $0.02 each; $10 is 500


def test_budget_steps_slow_then_refuse_wakes_then_stop(meter):
    assert meter.step() == "ok" and fu.slowed(300) == 300
    fill(meter, 250)
    assert meter.step() == "slow" and fu.slowed(300) == 600
    meter.guard("GET", f"{BASE}/api/1/vehicles/{VIN}/vehicle_data")
    fill(meter, 150)
    assert meter.step() == "tight" and fu.slowed(300) == 1200
    with pytest.raises(fu.Overspent, match="Waking"):
        meter.guard("POST", f"{BASE}/api/1/vehicles/{VIN}/wake_up")
    meter.guard("GET", f"{BASE}/api/1/vehicles/{VIN}/vehicle_data")  # data still goes while tight
    fill(meter, 100)
    assert meter.step() == "stopped"
    for url in (f"{BASE}/api/1/vehicles/{VIN}/vehicle_data", f"{BASE}/api/1/dx/charging/history", f"{BASE}/api/1/energy_sites/1/live_status"):
        with pytest.raises(fu.Overspent):
            meter.guard("GET", url)
    meter.credit = 20.0  # the owner raises it
    assert meter.step() == "slow"


def test_through_refuses_before_sending(meter):
    fill(meter, 500)
    sent = []
    f = tesla_fleet.Fleet("id", "secret", call=lambda *a, **k: sent.append(1) or (200, {"response": []}))
    with pytest.raises(tesla_fleet.TeslaError, match="paused"):
        f.vehicle_config("t", BASE, VIN)
    assert sent == []
    f.refresh  # sign-in is never held back
    f._send("POST", tesla_fleet.TOKEN, data={})
    assert sent == [1]


def test_commands_by_hand_go_through_past_the_limit_with_a_warning(meter):
    fill(meter, 500)
    ok = (200, {"response": {"result": True}})
    p = tesla_cmd.Proxy("https://p:4443", call=answers(ok), sleep=lambda s: None)
    assert p.send("t", VIN, "charge_stop").ok  # a command is never held back
    assert meter.warning() and "used up" in meter.warning()
    # a wake inside a hand-sent command goes; the same wake in the background does not
    with fu.by_hand():
        meter.guard("POST", f"{BASE}/api/1/vehicles/{VIN}/wake_up")
    with pytest.raises(fu.Overspent):
        meter.guard("POST", f"{BASE}/api/1/vehicles/{VIN}/wake_up")
    meter.on = False
    meter.guard("POST", f"{BASE}/api/1/vehicles/{VIN}/wake_up")  # budget off: nothing held


def test_the_80_percent_alert_goes_once(meter):
    conn, sent = Conn(), []
    fill(meter, 399)
    meter.tick(conn, sent.append)
    assert sent == []
    fill(meter, 1)
    meter.tick(conn, sent.append)
    fill(meter, 30)
    meter.tick(conn, sent.append)
    assert len(sent) == 1 and "80%" in sent[0] and "$8.00 of $10.00" in sent[0] and "slowing" in sent[0]
    assert sum(1 for q, _ in conn.sql if "INSERT INTO cc_fleet_alerts" in q) == 1
    # a restart in the same month remembers
    again = fu.Meter(clock=meter.clock)
    again.load(Conn(usage=[{"month": "2026-10", "vin": VIN, "category": "wakes", "calls": 430}], alerts=[{"month": "2026-10"}]))
    again.count("wakes", "wake_up", VIN, 200)
    again.tick(Conn(), sent.append)
    assert len(sent) == 1
    # next month it can speak again
    meter.clock.t = at(2026, 11)
    fill(meter, 400)
    meter.tick(conn, sent.append)
    assert len(sent) == 2


def test_tick_writes_the_batch_and_trims_the_log(meter):
    conn = Conn()
    meter.count("commands", "honk_horn", VIN, 200)
    meter.count("commands", "flash_lights", VIN, 200)
    meter.tick(conn)
    upserts = [p for q, p in conn.sql if "INTO cc_fleet_usage" in q]
    assert upserts == [("2026-10", VIN, "commands", 2)]
    assert sum(1 for q, _ in conn.sql if "INTO cc_fleet_calls" in q) == 2 and any("DELETE FROM cc_fleet_calls" in q for q, _ in conn.sql)
    conn2 = Conn()
    meter.tick(conn2)
    assert not [q for q, _ in conn2.sql if "INSERT" in q]  # nothing new, nothing written


def test_the_view_has_the_month_the_strip_and_the_last_calls(meter):
    meter.load(Conn(usage=[{"month": "2026-08", "vin": VIN, "category": "data", "calls": 1000}, {"month": "2026-10", "vin": VIN, "category": "commands", "calls": 40}]))
    meter.count("wakes", "wake_up", VIN, 200)
    v = meter.view({VIN: "Alpha"})
    assert v["month"] == "October 2026" and v["spend"] == pytest.approx(0.06)
    assert len(v["strip"]["bars"]) == 12 and v["strip"]["bars"][-1]["current"]
    assert "Aug 2026: $2.00" in [b["label"] for b in v["strip"]["bars"]][-3]
    assert v["recent"][0]["car"] == "Alpha" and v["recent"][0]["category"] == "Wakes"


def test_settings_configure_the_rates_and_the_credit(meter):
    meter.configure({"fleet_credit": "25", "fleet_rates": {"data": 0.004, "bogus": 1, "wakes": -1}, "fleet_budget_on": False})
    assert meter.credit == 25 and meter.rates["data"] == 0.004 and meter.rates["wakes"] == 0.02 and meter.on is False
    import settingsform

    out = settingsform.commands_form({"fleet_shown": "1", "fleet_credit": "$12.50", "fleet_rate_data": "0.003", "fleet_rate_wakes": "junk"}, {})
    assert out["fleet_credit"] == 12.5 and out["fleet_budget_on"] is False and out["fleet_rates"]["data"] == 0.003 and out["fleet_rates"]["wakes"] == 0.02


def test_settings_commands_shows_the_usage(meter, bare, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bare, "current_settings", lambda: {**bare.settings_mod.DEFAULTS})
    meter.count("data", "vehicle_data", VIN, 200)
    meter.count("commands", "honk_horn", VIN, 200)
    r = client.get("/settings/commands")
    assert r.status_code == 200
    for needle in ("Tesla API", "of $10", "Data requests", "vehicle_data", "honk_horn", 'name="fleet_credit"', "Hold back near the limit", "fleet-strip"):
        assert needle in r.text, needle


def test_the_command_route_warns_past_the_limit(site, client, meter):  # noqa: F811
    fill(meter, 500)
    r = client.post(f"/cars/{TESLA}/command", json={"command": "honk"}, headers=csrf(site["app"]))
    assert r.status_code == 200 and r.json()["ok"] and "allowance is used up" in r.json()["said"]
    assert site["proxy"].sent


def test_a_second_alert_goes_once_when_the_allowance_is_used_up(meter):
    conn, sent = Conn(), []
    fill(meter, 400)
    meter.tick(conn, sent.append)
    fill(meter, 100)
    meter.tick(conn, sent.append)
    fill(meter, 50)
    meter.tick(conn, sent.append)
    assert len(sent) == 2 and "used up" in sent[1] and "commands still go" in sent[1]
    assert [p for q, p in conn.sql if "INTO cc_fleet_alerts" in q] == [("2026-10",), ("2026-10:stop",)]


def test_an_alerted_month_loaded_from_disk_stays_quiet_even_crossing_both_lines(meter):
    meter.load(Conn(usage=[{"month": "2026-10", "vin": VIN, "category": "wakes", "calls": 300}], alerts=[{"month": "2026-10"}, {"month": "2026-10:stop"}]))
    fill(meter, 300)
    assert meter.outbox == [] and meter.step() == "stopped"


def test_a_failed_tick_puts_the_batch_and_the_alert_back(meter):
    class Boom(Conn):
        def commit(self):
            raise RuntimeError("down")

    sent = []
    fill(meter, 400)
    with pytest.raises(RuntimeError):
        meter.tick(Boom(), sent.append)
    assert sent == [] and len(meter.pending) == 400 and meter.pending_n == {("2026-10", VIN, "wakes"): 400} and meter.new_alerts == ["2026-10"] and len(meter.outbox) == 1
    good = Conn()
    meter.tick(good, sent.append)
    assert len(sent) == 1 and [p for q, p in good.sql if "INTO cc_fleet_usage" in q] == [("2026-10", VIN, "wakes", 400)] and meter.idle()


def test_an_idle_meter_has_nothing_to_write(meter):
    assert meter.idle()
    meter.count("data", "vehicle_data", VIN, 200)
    assert not meter.idle()
    meter.tick(Conn())
    assert meter.idle()


def test_counting_and_stepping_from_many_threads_never_trips_on_a_new_key(meter):
    import threading

    errors, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                meter.step()
                meter.view({})
                meter.guard("GET", f"{BASE}/api/1/vehicles/{VIN}/vehicle_data")
            except Exception as err:  # noqa: BLE001
                errors.append(err)
                return

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    for i in range(3000):
        meter.count("data", "vehicle_data", f"VIN{i:014d}", 200)
    stop.set()
    for t in readers:
        t.join()
    assert errors == [] and meter.calls()["data"] == 3000


def test_the_running_month_total_matches_the_per_car_counts(meter):
    meter.count("data", "vehicle_data", VIN, 200)
    meter.count("data", "vehicle_data", "OTHER000000000001", 200)
    meter.signals(VIN, 1500)
    assert meter.calls()["data"] == 2 and meter.calls(vin=VIN) == {"data": 1, "commands": 0, "wakes": 0, "signals": 1500, "other": 0}
    assert meter.spend() == pytest.approx(0.004 + 0.01)


def test_counts_made_before_the_load_are_kept_on_disk_after_it(meter):
    # the order in the site's startup is load first; a call counted before it is still written
    meter.count("data", "vehicle_data", VIN, 200)
    meter.load(Conn(usage=[{"month": "2026-10", "vin": VIN, "category": "data", "calls": 10}]))
    assert meter.calls()["data"] == 11 and meter.calls(vin=VIN)["data"] == 11
    conn = Conn()
    meter.tick(conn)
    assert [p for q, p in conn.sql if "INTO cc_fleet_usage" in q] == [("2026-10", VIN, "data", 1)]


def test_streamed_signals_are_counted_for_every_car_the_receiver_hears(meter):
    import json

    import tesla_live
    from test_tesla_live import ALPHA, BRAVO, frames, payload, stream

    s = stream(vins=(ALPHA,))
    record = payload(1, VehicleSpeed=10.0)
    s.handle(frames(record))
    s.handle(frames(payload(2, vin=BRAVO, VehicleSpeed=10.0)))  # not chosen, still billed by Tesla
    s.handle(frames({"vin": ALPHA, "alerts": []}, "alerts"))  # not signals
    n = len(json.loads(json.dumps(record))["data"])
    assert meter.calls()["signals"] == 2 * n and meter.calls(vin=BRAVO)["signals"] == n
    assert tesla_live and meter.spend() == pytest.approx(2 * n / 150000)


def test_a_zero_allowance_turns_the_budget_off(meter):
    meter.configure({"fleet_credit": 0})
    assert meter.on is False and meter.step() == "ok"
    fill(meter, 10)
    meter.guard("GET", f"{BASE}/api/1/vehicles/{VIN}/vehicle_data")
    assert meter.view({})["on"] is False and meter.outbox == []


def test_the_view_leads_with_the_costliest_row_and_projects_the_month(meter):
    meter.clock.t = at(2026, 10, 11)
    for _ in range(50):
        meter.count("data", "vehicle_data", VIN, 200)
    for _ in range(10):
        meter.count("wakes", "wake_up", VIN, 200)
    v = meter.view({VIN: "Alpha"})
    assert [r["key"] for r in v["rows"]] == ["wakes", "data", "commands"]  # signals and account rows with nothing are left out
    assert v["pace"] == pytest.approx(v["spend"] / (10.5 / 31), rel=0.02) and v["strip"]["ref"] is not None
    meter.clock.t = at(2026, 10, 1)
    assert meter.view({})["pace"] is None


def test_the_strip_draws_the_allowance_as_a_rule_and_scales_to_it():
    import strips

    s = strips.strip([{"value": 2.0, "label": "a"}, {"value": 0.0, "label": "b"}], ref=10)
    assert s["ref"] == pytest.approx(1.0) and s["bars"][0]["h"] == pytest.approx(0.2 * (strips.H - 1))
    assert strips.strip([{"value": 2.0, "label": "a"}])["ref"] is None


@pytest.mark.parametrize(
    ("raw", "want"),
    [("$12.50", 12.5), (" 7 ", 7.0), ("0", 0.0), ("", None), ("-1", None), ("1e9", None), ("abc", None), ("nan", None)],
)
def test_money_fields_read_only_sane_amounts(raw, want):
    import settingsform

    assert settingsform.money_field(raw) == want


def test_a_form_without_the_marker_leaves_the_budget_alone_and_signals_are_per_thousand():
    import settingsform

    assert settingsform.commands_form({"commands_shown": "1"}, {}) == {"commands_on": False}
    out = settingsform.commands_form({"fleet_shown": "1", "fleet_budget_on": "on", "fleet_credit": "-5", "fleet_rate_signals": "0.01"}, {})
    assert out["fleet_budget_on"] is True and "fleet_credit" not in out and out["fleet_rates"]["signals"] == pytest.approx(0.00001)


def test_every_background_worker_stretches_its_wait_by_the_budget(meter, monkeypatch):
    monkeypatch.setattr(meter, "factor", lambda: 4)
    from datetime import timedelta

    import billed
    import energy_cloud
    import identity
    import test_energy_cloud as tec
    import test_identity as ti

    # the Powerwall's live read
    f = tec.Fleet(live={"solar_power": 1, "battery_power": 0, "grid_power": 0, "load_power": 1, "percentage_charged": 50})
    w, *_ = tec.cloud(f)
    w.fallback(tec.NOW)
    w.fallback(tec.NOW + timedelta(minutes=19))
    assert f.asked == ["live"]
    w.fallback(tec.NOW + timedelta(minutes=21))
    assert f.asked == ["live", "live"]
    # the backfill's gap between days
    gaps = []
    day = tec.NOW.date() - timedelta(days=1)
    f2 = tec.Fleet(days={day: tec.day_series(day)})
    w2, state, *_ = tec.cloud(f2)
    w2.sleep = gaps.append
    w2.start_backfill(day)
    w2.backfill_step()
    assert gaps == [energy_cloud.STEP_GAP_S * 4]
    # the car identity's day and list gap
    store = ti.Store({ti.T1: {"vin": ti.T1, "detected": {}, "checked_at": ti.NOW - timedelta(hours=30)}})
    wi, _, _ = ti.worker(ti.FakeFleet([]), store, monkeypatch, garage=(ti.T1,))
    assert wi.due(store.rows, ti.NOW) == []  # read 30 hours ago: a day is four days now
    assert identity.DAY * 4 > timedelta(hours=30)
    # the bills' round
    waits = []

    class Stop:
        def wait(self, s):
            waits.append(s)

        def is_set(self):
            return len(waits) >= 2

    b = billed.BilledWorker(connect=None, access=None, fleet=None, vins=None, clear=None, interval_s=3600)
    b.stop, b.round = Stop(), lambda: None
    b.run()
    assert waits == [180, 3600 * 4]


def test_the_rate_fields_take_any_value_and_the_switch_shows_the_stored_setting(meter, bare, client, monkeypatch):  # noqa: F811
    import re

    monkeypatch.setattr(bare, "current_settings", lambda: {**bare.settings_mod.DEFAULTS, "fleet_credit": 0, "fleet_budget_on": True})
    meter.configure({"fleet_credit": 0, "fleet_budget_on": True})
    r = client.get("/settings/commands")
    rates = re.findall(r'<input type="number"[^>]*name="fleet_rate_[a-z]+"[^>]*>', r.text)
    assert len(rates) == 4 and all('step="any"' in t for t in rates)
    assert "0.00666667" in r.text
    assert re.search(r'name="fleet_budget_on"[^>]*checked', r.text) or re.search(r'checked[^>]*name="fleet_budget_on"', r.text)

"""The Fleet API client against canned answers: what it sends, what it hands back."""

from urllib.parse import parse_qs, urlparse

import pytest

import tesla_fleet
from tesla_fleet import NA, Fleet, TeslaError


class Fake:
    """Records each request and answers from a queue of (status, body)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, method, url, *, headers, data=None, json=None):
        self.sent.append({"method": method, "url": url, "headers": headers, "data": data, "json": json})
        return self.answers.pop(0)


def fleet(*answers):
    fake = Fake(*answers)
    return Fleet("client-alpha", "ta-secret.springfield", call=fake), fake


def test_the_sign_in_link_asks_for_the_read_scopes_and_the_charging_history_one_and_carries_the_state():
    f, _ = fleet()
    url = f.authorize_url("https://example.test/settings/sources/tesla/callback", "st8")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(tesla_fleet.AUTHORIZE)
    assert q["client_id"] == ["client-alpha"] and q["response_type"] == ["code"] and q["state"] == ["st8"]
    assert q["redirect_uri"] == ["https://example.test/settings/sources/tesla/callback"]
    assert q["scope"][0].split() == ["openid", "offline_access", "vehicle_device_data", "vehicle_location", "vehicle_charging_cmds"]


def test_a_code_is_exchanged_with_the_secret_and_the_expiry_becomes_a_time(monkeypatch):
    monkeypatch.setattr(tesla_fleet.time, "time", lambda: 1000.0)
    f, fake = fleet((200, {"access_token": "acc", "refresh_token": "ref", "expires_in": 28800}))
    out = f.exchange("code-1", "https://example.test/cb")
    sent = fake.sent[0]
    assert sent["method"] == "POST" and sent["url"] == tesla_fleet.TOKEN
    assert sent["data"] == {
        "grant_type": "authorization_code",
        "client_id": "client-alpha",
        "client_secret": "ta-secret.springfield",
        "code": "code-1",
        "audience": NA,
        "redirect_uri": "https://example.test/cb",
    }
    assert out == {"access_token": "acc", "refresh_token": "ref", "expires_at": 1000.0 + 28800}


def test_a_refresh_hands_back_the_new_refresh_token():
    f, fake = fleet((200, {"access_token": "acc2", "refresh_token": "ref2", "expires_in": 60}))
    out = f.refresh("ref1")
    assert fake.sent[0]["data"] == {"grant_type": "refresh_token", "client_id": "client-alpha", "refresh_token": "ref1"}
    assert out["refresh_token"] == "ref2" and out["access_token"] == "acc2"


def test_the_partner_token_is_a_client_credentials_grant():
    f, fake = fleet((200, {"access_token": "partner", "expires_in": 28800}))
    assert f.partner_token() == "partner"
    d = fake.sent[0]["data"]
    assert d["grant_type"] == "client_credentials" and d["client_secret"] == "ta-secret.springfield" and d["audience"] == NA
    assert d["scope"].split() == ["openid", "vehicle_device_data", "vehicle_location"]


def test_registering_posts_the_domain_with_the_partner_token():
    f, fake = fleet((200, {"access_token": "partner", "expires_in": 1}), (200, {"response": {"domain": "example.test"}}))
    f.register("example.test")
    sent = fake.sent[1]
    assert sent["url"] == f"{NA}/api/1/partner_accounts" and sent["json"] == {"domain": "example.test"}
    assert sent["headers"]["Authorization"] == "Bearer partner"


def test_the_registered_key_is_read_back_and_none_when_unknown():
    f, fake = fleet((200, {"access_token": "partner", "expires_in": 1}), (200, {"response": {"public_key": "04abc"}}))
    assert f.registered_key("example.test") == "04abc"
    assert "domain=example.test" in fake.sent[1]["url"]
    f, _ = fleet((200, {"access_token": "partner", "expires_in": 1}), (404, {"error": "not found"}))
    assert f.registered_key("example.test") is None


def test_the_region_names_the_accounts_own_base():
    f, fake = fleet((200, {"response": {"region": "eu", "fleet_api_base_url": "https://fleet-api.prd.eu.vn.cloud.tesla.com"}}))
    assert f.region("acc") == "https://fleet-api.prd.eu.vn.cloud.tesla.com"
    assert fake.sent[0]["url"] == f"{NA}/api/1/users/region" and fake.sent[0]["headers"]["Authorization"] == "Bearer acc"


def test_vehicles_are_named():
    f, _ = fleet((200, {"response": [{"vin": "5YJ3E1EA0JF000001", "display_name": "Alpha", "state": "asleep"}]}))
    assert f.vehicles("acc", NA) == [{"vin": "5YJ3E1EA0JF000001", "name": "Alpha", "state": "asleep"}]


def test_fleet_status_says_which_cars_hold_the_key():
    body = {
        "response": {
            "key_paired_vins": ["5YJ3E1EA0JF000001"],
            "unpaired_vins": ["5YJ3E1EA0JF000002"],
            "vehicle_info": {
                "5YJ3E1EA0JF000001": {"firmware_version": "2026.32.5", "total_number_of_keys": 6, "fleet_telemetry_version": "1.1.0"},
                "5YJ3E1EA0JF000002": {"firmware_version": "2026.20.1", "total_number_of_keys": 4},
            },
        }
    }
    f, fake = fleet((200, body))
    out = f.fleet_status("acc", NA, ["5YJ3E1EA0JF000001", "5YJ3E1EA0JF000002"])
    assert fake.sent[0]["json"] == {"vins": ["5YJ3E1EA0JF000001", "5YJ3E1EA0JF000002"]}
    assert out["5YJ3E1EA0JF000001"] == {"paired": True, "firmware": "2026.32.5", "keys": 6, "telemetry": "1.1.0"}
    assert out["5YJ3E1EA0JF000002"] == {"paired": False, "firmware": "2026.20.1", "keys": 4, "telemetry": None}


def test_a_refusal_raises_with_the_secret_masked():
    f, _ = fleet((401, {"error": "invalid_client", "error_description": "client ta-secret.springfield is not allowed"}))
    with pytest.raises(TeslaError) as e:
        f.partner_token()
    assert "ta-secret.springfield" not in str(e.value) and "invalid_client" in str(e.value) and e.value.status == 401


def test_an_answer_that_is_not_json_names_the_status():
    f, _ = fleet((502, None))
    with pytest.raises(TeslaError) as e:
        f.vehicles("acc", NA)
    assert "502" in str(e.value)


def test_a_network_failure_is_a_tesla_error_not_a_crash():
    import httpx

    def down(method, url, *, headers, data=None, json=None):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(TeslaError) as e:
        Fleet("client-alpha", "ta-secret.springfield", call=down).partner_token()
    assert "did not answer" in str(e.value) and "ConnectTimeout" in str(e.value)


def test_a_refused_refresh_never_repeats_the_refresh_token():
    f, _ = fleet((400, {"error": "invalid_grant", "error_description": "refresh token rt-springfield-123 is spent"}))
    with pytest.raises(TeslaError) as e:
        f.refresh("rt-springfield-123")
    assert "rt-springfield-123" not in str(e.value) and "invalid_grant" in str(e.value)


def test_a_refused_code_never_repeats_the_code():
    f, _ = fleet((400, {"error": "invalid_grant", "error_description": "code abc-code-9 expired"}))
    with pytest.raises(TeslaError) as e:
        f.exchange("abc-code-9", "https://example.test/cb")
    assert "abc-code-9" not in str(e.value)


def test_a_token_answer_without_a_token_is_a_refusal_not_bearer_none():
    f, _ = fleet((200, {"token_type": "Bearer"}))
    with pytest.raises(TeslaError):
        f.partner_token()
    f, _ = fleet((200, {"expires_in": 60}))
    with pytest.raises(TeslaError):
        f.refresh("ref1")


# ---- streaming config. Answers in the shapes Tesla's docs and Fleet API clients show.
ALPHA, BRAVO = "5YJ3E1EA0JF000001", "5YJ3E1EA0JF000002"
BASE = "https://fleet-api.example.test"


def test_a_signed_config_goes_to_the_jws_endpoint_and_says_who_was_skipped():
    f, fake = fleet((200, {"response": {"updated_vehicles": 1, "skipped_vehicles": {"missing_key": [BRAVO], "unsupported_firmware": [], "max_configs": []}}}))
    out = f.telemetry_create("acc", BASE, [ALPHA, BRAVO], "h.p.s")
    sent = fake.sent[0]
    assert sent["method"] == "POST" and sent["url"] == f"{BASE}/api/1/vehicles/fleet_telemetry_config_jws"
    assert sent["json"] == {"vins": [ALPHA, BRAVO], "token": "h.p.s"} and sent["headers"] == {"Authorization": "Bearer acc"}
    assert out == {"updated": 1, "skipped": {"missing_key": [BRAVO]}}


def test_a_cars_config_reads_synced_limit_and_key():
    body = {"response": {"synced": True, "limit_reached": False, "key_paired": True, "config": {"hostname": "stream.example.test", "port": 8443, "exp": 1790000000, "ca": "..."}}}
    f, fake = fleet((200, body))
    got = f.telemetry_config("acc", BASE, ALPHA)
    assert fake.sent[0]["url"] == f"{BASE}/api/1/vehicles/{ALPHA}/fleet_telemetry_config" and fake.sent[0]["method"] == "GET"
    assert got == {"synced": True, "limit_reached": False, "key_paired": True, "hostname": "stream.example.test", "port": 8443, "exp": 1790000000, "fields": None}


@pytest.mark.parametrize("answer", [(404, None), (200, {"response": {"synced": False, "config": None}}), (200, {"response": {}})])
def test_no_config_at_tesla_reads_as_none(answer):
    f, _ = fleet(answer)
    assert f.telemetry_config("acc", BASE, ALPHA) is None


def test_stopping_deletes_the_cars_config_and_a_missing_one_is_fine():
    f, fake = fleet((200, {"response": {"updated_vehicles": 1}}), (404, None))
    f.telemetry_delete("acc", BASE, ALPHA)
    f.telemetry_delete("acc", BASE, ALPHA)
    assert [s["method"] for s in fake.sent] == ["DELETE", "DELETE"] and fake.sent[0]["url"].endswith(f"/vehicles/{ALPHA}/fleet_telemetry_config")


def test_the_cars_reported_errors_are_read_for_the_domain_newest_first():
    body = {
        "response": {
            "fleet_telemetry_errors": [
                {"name": "evotlb-connectivity", "error": "dial tcp: i/o timeout", "vin": ALPHA, "created_at": "2026-10-03T10:00:00Z"},
                {"name": "evotlb-tls", "error": "x509: certificate signed by unknown authority", "vin": ALPHA, "created_at": "2026-10-04T10:00:00Z"},
            ]
        }
    }
    f, fake = fleet((200, {"access_token": "partner"}), (200, body))
    got = f.telemetry_errors("stream.example.test")
    assert fake.sent[1]["url"] == f"{NA}/api/1/partner_accounts/fleet_telemetry_errors?domain=stream.example.test"
    assert fake.sent[1]["headers"] == {"Authorization": "Bearer partner"}
    assert [e["name"] for e in got] == ["evotlb-tls", "evotlb-connectivity"] and got[0]["vin"] == ALPHA and "x509" in got[0]["error"]


def test_a_config_refusal_is_a_tesla_error_with_the_token_masked():
    f, _ = fleet((403, {"error": "unauthorized", "error_description": "token acc-secret-token has no access to location"}))
    with pytest.raises(TeslaError) as err:
        f.telemetry_create("acc-secret-token", BASE, [ALPHA], "h.p.s")
    assert err.value.status == 403 and "acc-secret-token" not in str(err.value)


# ---- the Powerwall, through the same account


def test_products_lists_the_energy_sites_and_nothing_else():
    f, fake = fleet(
        (
            200,
            {
                "response": [
                    {"vin": "5YJ3E1EA0SF000001", "id": 1},
                    {"energy_site_id": 1234567890, "resource_type": "battery", "site_name": "Springfield", "id": "abc"},
                    {"energy_site_id": 55, "resource_type": "solar"},
                ]
            },
        )
    )
    got = f.energy_sites("tok", NA)
    assert fake.sent[0]["url"] == f"{NA}/api/1/products" and fake.sent[0]["headers"] == {"Authorization": "Bearer tok"}
    assert got == [{"site_id": "1234567890", "name": "Springfield", "kind": "battery"}, {"site_id": "55", "name": "", "kind": "solar"}]


def test_live_status_and_site_info_are_the_responses_unwrapped():
    f, fake = fleet(
        (
            200,
            {"response": {"solar_power": 100, "battery_power": -50, "grid_power": 10, "load_power": 60, "percentage_charged": 80.5, "grid_status": "Active", "timestamp": "2026-07-14T19:00:00-07:00"}},
        ),
        (200, {"response": {"installation_date": "2024-03-01T00:00:00-08:00"}}),
    )
    assert f.energy_live("tok", NA, "1234567890")["load_power"] == 60
    assert f.energy_installed("tok", NA, "1234567890") == "2024-03-01"
    assert fake.sent[0]["url"] == f"{NA}/api/1/energy_sites/1234567890/live_status"
    assert fake.sent[1]["url"] == f"{NA}/api/1/energy_sites/1234567890/site_info"


def test_calendar_history_asks_for_one_local_day_of_power_with_the_token_in_a_header_only():
    from datetime import date

    f, fake = fleet((200, {"response": {"time_series": [{"timestamp": "2026-07-14T00:00:00-07:00", "solar_power": 0}]}}))
    got = f.energy_power_day("tok", NA, "1234567890", date(2026, 7, 14), "America/Los_Angeles")
    url = fake.sent[0]["url"]
    q = parse_qs(urlparse(url).query)
    assert got == [{"timestamp": "2026-07-14T00:00:00-07:00", "solar_power": 0}]
    assert urlparse(url).path == "/api/1/energy_sites/1234567890/calendar_history" and "tok" not in url
    assert q["kind"] == ["power"] and q["time_zone"] == ["America/Los_Angeles"]
    assert q["start_date"] == ["2026-07-14T00:00:00-07:00"] and q["end_date"] == ["2026-07-14T23:59:59-07:00"]


def test_a_site_id_that_is_not_a_number_never_reaches_a_url():
    f, fake = fleet()
    from datetime import date

    for bad in ("12/../vehicles", "", "1;2", "abc"):
        with pytest.raises(TeslaError):
            f.energy_live("tok", NA, bad)
        with pytest.raises(TeslaError):
            f.energy_power_day("tok", NA, bad, date(2026, 7, 14), "UTC")
    assert fake.sent == []


def test_a_token_without_the_energy_scope_is_a_refusal_the_caller_can_name():
    f, _ = fleet((403, {"error": "forbidden: missing scope energy_device_data"}))
    with pytest.raises(TeslaError) as e:
        f.energy_live("tok", NA, "1")
    assert e.value.status == 403


def test_the_energy_sign_in_asks_for_the_vehicle_scopes_and_the_energy_one_together_and_the_normal_one_never_does():
    f, _ = fleet()
    plain = parse_qs(urlparse(f.authorize_url("https://example.test/cb", "st8")).query)["scope"][0].split()
    both = parse_qs(urlparse(f.authorize_url("https://example.test/cb", "st8", energy=True)).query)["scope"][0].split()
    assert "energy_device_data" not in plain and "energy_device_data" not in tesla_fleet.SCOPES.split()
    assert both == plain + ["energy_device_data"], "one token covers the cars and the Powerwall"


def test_the_energy_sign_in_asks_tesla_to_prompt_for_a_scope_never_granted_before():
    """An account that consented once, to the vehicle scopes, is not asked again by default:
    Tesla hands back the old grant and drops energy_device_data without a word."""
    f, _ = fleet()
    q = parse_qs(urlparse(f.authorize_url("https://example.test/cb", "st8", energy=True)).query)
    assert q.get("prompt_missing_scopes") == ["true"]

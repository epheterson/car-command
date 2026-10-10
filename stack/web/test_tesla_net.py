"""What a car is told to send and where, and the receiver check a car would make."""

import re
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes

import tesla_net

NOW = 1_790_000_000.0
HOST = "stream.example.test"


def test_the_roots_are_lets_encrypts_two():
    certs = x509.load_pem_x509_certificates(tesla_net.ROOTS.encode())
    assert [c.fingerprint(hashes.SHA256()).hex() for c in certs] == [
        "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6",  # ISRG Root X1
        "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470",  # ISRG Root X2
    ]


def test_the_config_points_at_the_receiver_and_ends_in_a_year():
    c = tesla_net.config(HOST, now=NOW)
    assert c["hostname"] == HOST and c["port"] == 8443 and c["ca"] == tesla_net.ROOTS
    assert c["exp"] == int(NOW) + 360 * 86400 and c["alert_types"] == ["service"]
    assert c["fields"]["Location"] == {"interval_seconds": 1, "minimum_delta": 3} and c["fields"]["Version"] == {"interval_seconds": 3600}


def test_every_field_the_stream_reads_is_asked_for():
    src = (Path(__file__).parent / "tesla_stream.py").read_text()
    read = set(re.findall(r'c\.get\("([A-Za-z]+)"', src))
    assert read and read <= set(tesla_net.FIELDS), read - set(tesla_net.FIELDS)


@pytest.mark.parametrize("host,ok", [(HOST, True), ("carcommand-stream.tail0000.ts.net", True), ("", False), ("http://x.test", False), ("x.test:8443", False), ("no_dots", False), (None, False)])
def test_a_stream_address_is_a_plain_name(host, ok):
    assert tesla_net.valid_host(host) is ok


def shake_with(result):
    def shake(host, port, ca):
        assert (host, port, ca) == (HOST, 8443, tesla_net.ROOTS)
        if isinstance(result, BaseException):
            raise result
        return result

    return shake


def stamp(epoch: float) -> str:
    import time

    return time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(epoch))


def test_a_trusted_certificate_with_time_left_is_reachable():
    got = tesla_net.reach(HOST, shake=shake_with({"notAfter": stamp(NOW + 60 * 86400)}), now=NOW)
    assert got["ok"] and got["problem"] is None and abs(got["expires"] - (NOW + 60 * 86400)) < 1


def test_a_certificate_near_its_end_is_reachable_but_says_so():
    got = tesla_net.reach(HOST, shake=shake_with({"notAfter": stamp(NOW + 5 * 86400 + 60)}), now=NOW)
    assert got["ok"] and got["problem"] == "expiring" and "5 days" in got["error"]


def test_a_receiver_that_asks_for_a_cars_certificate_counts_as_reachable():
    err = ssl.SSLError(1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required")
    err.reason = "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    got = tesla_net.reach(HOST, shake=shake_with(err), now=NOW)
    assert got["ok"] and got["problem"] is None


def test_a_certificate_a_car_would_not_trust_is_the_problem():
    err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    err.verify_message = "self-signed certificate"
    got = tesla_net.reach(HOST, shake=shake_with(err), now=NOW)
    assert not got["ok"] and got["problem"] == "certificate" and "self-signed" in got["error"]


@pytest.mark.parametrize("err", [ConnectionRefusedError(), TimeoutError(), OSError("no route")])
def test_nothing_answering_is_unreachable(err):
    got = tesla_net.reach(HOST, shake=shake_with(err), now=NOW)
    assert not got["ok"] and got["problem"] == "unreachable" and f"{HOST}:8443" in got["error"]


def test_something_else_on_the_port_is_not_the_receiver():
    err = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
    err.reason = "WRONG_VERSION_NUMBER"
    got = tesla_net.reach(HOST, shake=shake_with(err), now=NOW)
    assert not got["ok"] and got["problem"] == "unreachable" and "not as the receiver" in got["error"]

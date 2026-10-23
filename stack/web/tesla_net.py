"""Where the cars stream to, and what each car is told to send.

The receiver (the `tesla` compose profile) listens behind a public name on port 8443 and
terminates the cars' mutual TLS itself, with a Let's Encrypt certificate for that name.
A car learns the address, the port, which certificates to trust and which fields to send
from one signed config (tesla_jws), built here. `reach` does what a car does first: a TLS
handshake to that name, trusting only the certificates the config names."""

from __future__ import annotations

import hashlib
import re
import socket
import ssl
import time

PORT = 8443
CONFIG_DAYS = 360  # a config carries its own end; Start again renews it
CERT_WARN_DAYS = 14  # the certificate renews at 30 days left; under 14 means a renewal did not reach the receiver
HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# What the history needs, and how often a car may send each while it changes: a second
# for the route and the power curve, longer for what moves slowly. A car sends a field
# only when its value changes, so a parked car sends almost nothing.
FIELDS = {
    "Location": 1,
    "GpsHeading": 1,
    "VehicleSpeed": 1,
    "Gear": 1,
    "PackVoltage": 1,
    "PackCurrent": 1,
    "DetailedChargeState": 1,
    "ChargeState": 1,
    "SentryMode": 1,
    "Odometer": 10,
    # Locked, doors and windows: sent when they change, at most this often. Names are Tesla's Field enum
    # (fleet-telemetry protos/vehicle_data.proto: DoorState 58, Locked 59, FdWindow..RpWindow 60-63).
    "Locked": 10,
    "DoorState": 10,
    "FdWindow": 60,
    "FpWindow": 60,
    "RdWindow": 60,
    "RpWindow": 60,
    "ACChargingPower": 10,
    "DCChargingPower": 10,
    "ChargerVoltage": 10,
    "ChargeAmps": 10,
    "FastChargerPresent": 10,
    "ACChargingEnergyIn": 30,
    "DCChargingEnergyIn": 30,
    "BatteryLevel": 60,
    "Soc": 60,
    "EstBatteryRange": 60,
    "RatedRange": 60,
    "EnergyRemaining": 60,
    "ChargeLimitSoc": 60,
    "FastChargerType": 60,
    "InsideTemp": 60,
    "OutsideTemp": 60,
    # Climate: on, preconditioning or Cabin Overheat Protection; the keeper (Dog, Camp), defrost,
    # the set temperature. Sent when they change.
    "HvacPower": 10,
    "ClimateKeeperMode": 60,
    "DefrostMode": 60,
    "CabinOverheatProtectionMode": 300,
    "HvacLeftTemperatureRequest": 60,
    "PreconditioningEnabled": 60,
    # Charging, beyond power: the port, its latch, the rate, time to full, the current asked for
    "ChargePortDoorOpen": 10,
    "ChargePortLatch": 10,
    "ChargeRateMilePerHour": 60,
    "TimeToFullCharge": 60,
    "ChargeCurrentRequest": 60,
    # Someone in the driver's seat; a software update waiting
    "DriverSeatOccupied": 10,
    "SoftwareUpdateVersion": 3600,
    "Version": 3600,
    "VehicleName": 3600,
    # What the car is (identity.py): model, trim, paint, wheels, roof. They change only with
    # the car, so a car sends them when it connects and almost never again.
    "CarType": 3600,
    "Trim": 3600,
    "ExteriorColor": 3600,
    "WheelType": 3600,
    "RoofColor": 3600,
    # The route the driver set in the car (tesla_nav): where to, sent when it changes; how
    # far and how long, every half minute while a route runs. Nothing is sent without one.
    # Not RouteLastUpdated: Tesla documents it as broken (it sends nothing).
    "DestinationName": 5,
    "DestinationLocation": 5,
    "OriginLocation": 60,
    "MinutesToArrival": 30,
    "MilesToArrival": 30,
}

# The smallest change worth sending, per field, as `minimum_delta` beside `interval_seconds`. Tesla bills each
# signal, and a number that jitters sends one every second though the site could not tell the difference.
# From Tesla's Fleet Telemetry documentation (developer.tesla.com/docs/fleet-api/fleet-telemetry): a field is sent
# once interval_seconds has elapsed AND its value has changed; Location takes a minimum_delta in metres (firmware
# 2025.2.6 on); and the billing page says to "utilize the minimum_delta configuration option for numeric fields".
# Nothing else is sent: resend_interval_seconds is not documented there. Enum fields (Gear, ChargeState,
# DetailedChargeState, SentryMode, Locked, DoorState) already send on change only, so they carry none.
#
# What each costs the site (test_tesla_cost.py judges them on a simulated drive and charge):
#   Location 3 m       the route is drawn from points 3 m apart or more: invisible, and a parked fix's jitter is
#                      silent. The recorder itself ignores moves under about a metre.
#   VehicleSpeed 0.5   the top speed can read up to half a mph low, under what the dash shows.
#   GpsHeading 5       the map arrow turns in steps of 5 degrees.
#   PackVoltage 1 V    with PackCurrent 2 A: the drive page's peak draw and regeneration and the charge curve move
#   PackCurrent 2 A    by under 2 % (of 100 A and more); the energy a held reading gives, under 1 %.
#   ACChargingPower 0.1 kW, DCChargingPower 1 kW, ChargerVoltage 2 V, ChargeAmps 1 A: the charge curve's steps.
DELTAS = {
    "Location": 3,
    "VehicleSpeed": 0.5,
    "GpsHeading": 5,
    "PackVoltage": 1,
    "PackCurrent": 2,
    "ACChargingPower": 0.1,
    "DCChargingPower": 1,
    "ChargerVoltage": 2,
    "ChargeAmps": 1,
}

# The roots the receiver's certificate chains to (Let's Encrypt's two, RSA and ECDSA), as
# the config's `ca`: a car trusts these for the receiver and nothing else. Roots rather
# than the current chain, so a renewal under a new intermediate needs no new config.
ROOTS = """\
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw
CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg
R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00
MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT
ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw
EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW
+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9
ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T
AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI
zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW
tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1
/q4AaOeMSQ+2b1tbFfLn
-----END CERTIFICATE-----
"""


def valid_host(host: str | None) -> bool:
    return bool(host) and bool(HOST.match(host))


def fields_version(fields: dict | None = None) -> str:
    """A short name for a field list and its intervals: what a car was last sent is kept by
    it, so a car whose config predates a change to FIELDS can be told so and sent the new one."""
    items = sorted((fields or FIELDS).items()) + sorted(DELTAS.items())
    return hashlib.sha256(repr(items).encode()).hexdigest()[:12]


def sent_fields(config: dict | None) -> dict | None:
    """The fields, with their intervals, of a config as Tesla holds it; None when it does not say."""
    got = (config or {}).get("fields")
    if not isinstance(got, dict) or not got:
        return None
    return {k: (v.get("interval_seconds") if isinstance(v, dict) else v) for k, v in got.items()}


def sent_deltas(config: dict | None) -> dict:
    """The minimum_delta of each field in a config as Tesla holds it; only those that carry one."""
    got = (config or {}).get("fields")
    if not isinstance(got, dict):
        return {}
    return {k: v["minimum_delta"] for k, v in got.items() if isinstance(v, dict) and v.get("minimum_delta") is not None}


def field_spec(fields: dict | None = None) -> dict:
    """The `fields` of a config: each field's interval, and its minimum_delta where it has one."""
    return {k: {"interval_seconds": v, **({"minimum_delta": DELTAS[k]} if k in DELTAS else {})} for k, v in (fields or FIELDS).items()}


def config(host: str, now: float | None = None, port: int = PORT, ca: str = ROOTS, fields: dict | None = None) -> dict:
    """The claims a car's signed config carries."""
    now = time.time() if now is None else now
    return {
        "hostname": host,
        "port": port,
        "ca": ca,
        "fields": field_spec(fields),
        "alert_types": ["service"],
        "prefer_typed": True,
        "exp": int(now) + CONFIG_DAYS * 86400,
    }


def handshake(host: str, port: int, ca: str, timeout: float = 10) -> dict:
    """The certificate the receiver shows, checked against `ca` and the name."""
    ctx = ssl.create_default_context(cadata=ca)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # the default already; said, so no older one is ever offered
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert() or {}


# The receiver asks every client for a car's certificate. A client without one is turned
# away after it has checked the receiver's own, so these refusals mean "reachable and trusted".
ASKED_FOR_A_CAR = ("CERTIFICATE_REQUIRED", "BAD_CERTIFICATE", "HANDSHAKE_FAILURE")


def reach(host: str, port: int = PORT, ca: str = ROOTS, shake=handshake, now: float | None = None) -> dict:
    """{ok, problem, error, expires}: problem is None, "unreachable", "certificate" or "expiring"."""
    now = time.time() if now is None else now
    where = f"{host}:{port}"
    try:
        cert = shake(host, port, ca)
    except ssl.SSLCertVerificationError as err:
        why = getattr(err, "verify_message", None) or str(err)
        return {"ok": False, "problem": "certificate", "error": f"{where} shows a certificate a car would not trust ({why}).", "expires": None}
    except ssl.SSLError as err:
        if any(r in str(getattr(err, "reason", "") or err).upper() for r in ASKED_FOR_A_CAR):
            return {"ok": True, "problem": None, "error": None, "expires": None}
        return {"ok": False, "problem": "unreachable", "error": f"{where} answered, but not as the receiver ({getattr(err, 'reason', None) or type(err).__name__}).", "expires": None}
    except (OSError, socket.timeout) as err:
        return {"ok": False, "problem": "unreachable", "error": f"{where} did not answer ({type(err).__name__}).", "expires": None}
    expires = ssl.cert_time_to_seconds(cert["notAfter"]) if cert.get("notAfter") else None
    if expires is not None and expires - now < CERT_WARN_DAYS * 86400:
        days = max(0, int((expires - now) // 86400))
        return {"ok": True, "problem": "expiring", "error": f"The receiver's certificate runs out in {days} day{'s' if days != 1 else ''}.", "expires": expires}
    return {"ok": True, "problem": None, "error": None, "expires": expires}

"""What Tesla's signing proxy needs on disk: the app's private key, and a TLS certificate
and key for the proxy's own HTTPS (made here, self-signed, for the names the site reaches
it by; the site pins exactly this certificate).

The app's key is the one Connect Tesla made and sealed in cc_state (tesla_keys). It is
written in the form the proxy reads (SEC1, "EC PRIVATE KEY"), 0600, for the proxy's user:

- the one container: the supervisor runs `python3 tesla_proxy.py export` as the site, reads
  the JSON it prints, writes the files as the proxy's own user and starts the proxy
  (stack/aio/carcommand.py, `carcommand tesla-proxy`);
- compose: the site writes them at start into TESLA_PROXY_DIR, a volume the proxy mounts
  read-only and reads as the site's own user id (`write_dir`).

The TLS key is a fresh P-256 key, never the app's: the proxy refuses to start on one that is.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAMES = ("localhost", "tesla-proxy")
CERT_DAYS = 825
KEY, CERT, TLS_KEY = "key.pem", "tls.crt", "tls.key"
NO_KEY = 3  # export's exit code while Connect Tesla has made no key yet


def proxy_key_pem(private_pem: str) -> str:
    """The app's key in the form the proxy reads."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()


def tls_pair(names=NAMES, now: datetime | None = None) -> tuple[str, str]:
    """(certificate, key) PEMs: self-signed, for `names` and 127.0.0.1."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = now or datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    alt = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem


def material(private_pem: str, names=NAMES) -> dict:
    cert, tls_key = tls_pair(names)
    return {KEY: proxy_key_pem(private_pem), CERT: cert, TLS_KEY: tls_key}


def _write(path: Path, text: str, mode: int, owner: tuple[int, int] | None) -> None:
    """Whole or not at all, with its mode (and owner) from the first byte."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        if owner is not None:
            os.fchown(fd, *owner)
        os.fchmod(fd, mode)
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_dir(directory: Path, files: dict, owner: tuple[int, int] | None = None) -> None:
    """The three files into `directory`: both keys 0600, the certificate 0644 (the site reads it to pin)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        _write(directory / name, text, 0o644 if name == CERT else 0o600, owner)


def keep_for_proxy(directory: Path, private_pem: str | None) -> bool:
    """Compose: the files the proxy reads, written when the key is new or changed. The
    certificate is made once and kept while the key stays the same."""
    if not private_pem:
        return False
    want = proxy_key_pem(private_pem)
    have = directory / KEY
    if have.exists() and have.read_text() == want and (directory / CERT).exists() and (directory / TLS_KEY).exists():
        return False
    write_dir(directory, material(private_pem, names=NAMES + tuple(n for n in (os.environ.get("TESLA_PROXY_NAME"),) if n)))
    return True


def stored_key(database_url: str, secret_key: str | None) -> str | None:
    """The app's key, opened from cc_state, or None before Connect Tesla made one."""
    import psycopg

    import sources
    import tesla_keys

    vault = sources.Vault(secret_key)
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        row = conn.execute("SELECT value FROM cc_state WHERE key = %s", (tesla_keys.STATE_KEY,)).fetchone()
    return tesla_keys.stored(lambda _k: row[0] if row else None, vault)


def main(argv: list[str]) -> int:
    if argv[1:] != ["export"]:
        print("usage: tesla_proxy.py export", file=sys.stderr)
        return 2
    try:
        pem = stored_key(os.environ["DATABASE_URL"], os.environ.get("CC_SECRET_KEY"))
    except Exception as err:  # noqa: BLE001 - the supervisor asks again in a minute
        print(f"could not read the app's key: {type(err).__name__}", file=sys.stderr)
        return 1
    if not pem:
        return NO_KEY
    json.dump(material(pem), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""The signing proxy's files: the app's key in the proxy's form, a TLS pair the site can
pin, written with the right modes, and the export the one container's supervisor reads."""

import json
import ssl
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

import tesla_keys
import tesla_proxy as tp


def test_the_app_key_is_written_as_sec1_and_is_the_same_key():
    pem = tesla_keys.new_private_pem()
    out = tp.proxy_key_pem(pem)
    assert out.startswith("-----BEGIN EC " + "PRIVATE KEY-----"), "SEC1, the form the proxy reads"
    assert tesla_keys.public_pem(out) == tesla_keys.public_pem(pem)


def test_the_tls_key_is_never_the_app_key():
    pem = tesla_keys.new_private_pem()
    files = tp.material(pem)
    assert tesla_keys.public_pem(files[tp.TLS_KEY]) != tesla_keys.public_pem(pem)


def test_the_site_can_pin_the_certificate_over_a_real_handshake(tmp_path):
    cert, key = tp.tls_pair()
    (tmp_path / "c.pem").write_text(cert)
    (tmp_path / "k.pem").write_text(key)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        r = httpx.get(f"https://localhost:{server.server_address[1]}/health", verify=ssl.create_default_context(cafile=str(tmp_path / "c.pem")), timeout=5)
        assert r.status_code == 200 and r.text == "OK"
    finally:
        server.server_close()


def test_files_are_written_with_their_modes(tmp_path):
    tp.write_dir(tmp_path / "p", tp.material(tesla_keys.new_private_pem()))
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in (tmp_path / "p").iterdir()}
    assert modes == {tp.KEY: 0o600, tp.TLS_KEY: 0o600, tp.CERT: 0o644}


def test_keep_for_proxy_writes_once_per_key(tmp_path):
    pem = tesla_keys.new_private_pem()
    assert not tp.keep_for_proxy(tmp_path, None)
    assert tp.keep_for_proxy(tmp_path, pem)
    cert = (tmp_path / tp.CERT).read_text()
    assert not tp.keep_for_proxy(tmp_path, pem), "the same key: the certificate stays the one the site pins"
    assert (tmp_path / tp.CERT).read_text() == cert
    assert tp.keep_for_proxy(tmp_path, tesla_keys.new_private_pem())


def test_export_prints_the_files_or_says_there_is_no_key_yet(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setattr(tp, "stored_key", lambda url, key: None)
    assert tp.main(["tesla_proxy.py", "export"]) == tp.NO_KEY
    pem = tesla_keys.new_private_pem()
    monkeypatch.setattr(tp, "stored_key", lambda url, key: pem)
    assert tp.main(["tesla_proxy.py", "export"]) == 0
    files = json.loads(capsys.readouterr().out)
    assert set(files) == {tp.KEY, tp.CERT, tp.TLS_KEY}
    assert tp.main(["tesla_proxy.py"]) == 2

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mapsclient import MapsClient, available, valid_region_id


def test_region_id_validation():
    assert valid_region_id("us/illinois")
    for bad in ("../../settings", "us/../x", "", "US/Illinois", "us/illinois!", "a" * 81, "/us"):
        assert not valid_region_id(bad), bad


def test_add_and_remove_reject_a_bad_id_without_ever_calling_out():
    class H(BaseHTTPRequestHandler):
        seen = []

        def do_POST(self):
            H.seen.append(("POST", self.path))
            raw = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_DELETE(self):
            H.seen.append(("DELETE", self.path))
            raw = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m = MapsClient(f"http://127.0.0.1:{srv.server_address[1]}")
        assert m.add("../../settings") is None and m.remove("us/../x") is None
        assert H.seen == []
        assert m.add("us/illinois") == {} and H.seen == [("POST", "/regions/add")]
    finally:
        srv.shutdown()


def test_no_url_means_no_maps():
    m = MapsClient(None)
    assert m.status() is None and m.candidates(39.8, -89.65) is None and not available(None)


def test_a_dead_service_is_none_not_an_error():
    assert MapsClient("http://127.0.0.1:9", timeout=0.5).status() is None


def test_status_round_trip():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps({"version": 3, "regions": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = MapsClient(f"http://127.0.0.1:{srv.server_address[1]}").status()
        assert s == {"version": 3, "regions": []} and available(s)
    finally:
        srv.shutdown()


def test_reverse_round_trip():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps({"address": {"road": "Oak Ave", "city": "Springfield"}, "display_name": "Oak Ave, Springfield"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = MapsClient(f"http://127.0.0.1:{srv.server_address[1]}").reverse(39.8, -89.65)
        assert r == {"address": {"road": "Oak Ave", "city": "Springfield"}, "display_name": "Oak Ave, Springfield"}
    finally:
        srv.shutdown()


def test_retry_posts_to_the_backoff_reset():
    class H(BaseHTTPRequestHandler):
        seen = []

        def do_POST(self):
            H.seen.append(self.path)
            raw = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = MapsClient(f"http://127.0.0.1:{srv.server_address[1]}").retry()
        assert r == {} and H.seen == ["/retry"]
    finally:
        srv.shutdown()


def test_a_region_shows_its_human_name():
    from mapsclient import region_name

    assert region_name({"id": "us/oregon", "name": "us/oregon"}) == "Oregon"
    assert region_name({"id": "us/new-york", "name": None}) == "New York"
    assert region_name({"id": "us/california/norcal", "name": "Northern California"}) == "Northern California"
    assert region_name({"id": "europe/monaco", "name": "Monaco"}) == "Monaco"
    assert region_name({"id": "europe/monaco"}) == "Monaco"


def test_a_region_reason_never_shows_a_coordinate():
    from mapsclient import region_reason

    assert region_reason("a drive near 39.8, -89.7") == "Your drives"
    assert region_reason("where your cars drive") == "Your drives"
    assert region_reason("added by hand") == "Added by you"
    assert region_reason("refresh") == "Refreshing"
    assert region_reason("") == ""
    assert region_reason(None) == ""

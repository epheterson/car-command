#!/usr/bin/env python3
"""Container healthcheck: GET the REST plugin's /healthcheck with basic auth."""

import base64
import os
import sys
import urllib.request

user, password = os.environ.get("REST_USER", ""), os.environ.get("REST_PASSWORD", "")
token = base64.b64encode(f"{user}:{password}".encode()).decode()
req = urllib.request.Request("http://127.0.0.1:40000/healthcheck", headers={"Authorization": f"Basic {token}"})
try:
    with urllib.request.urlopen(req, timeout=8) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:  # noqa: BLE001 - any failure is unhealthy
    sys.exit(1)

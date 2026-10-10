"""Print the setup code, while no password is set.

The code is what the first password needs from anywhere but the machine itself or, in the first hour, a device on the
home network. It also gates turning sign-in on from Settings and pinning Cloudflare Access. It is derived from the
site's signing key, so it is the same after a restart; once a password is set there is nothing for it to open, and
this prints a line saying so. Run as `make setup-code`."""

import os

import psycopg

import auth


def code(conn) -> str | None:
    if os.environ.get("CC_PASSWORD") or auth.store_get(conn, "password"):
        return None
    secret = os.environ.get("CC_SECRET") or auth.store_get(conn, "secret")
    return auth.setup_code(secret) if secret else None


if __name__ == "__main__":
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as conn:
        print(code(conn) or "no setup code: a password is already set (or the site has not started yet)")

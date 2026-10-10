"""Locked out of sign-in: forget the switch chosen in Settings and the Cloudflare Access pin.

The password is kept. After this the site starts as .env says: CC_AUTH decides whether sign-in is on, and
CF_ACCESS_TEAM with CF_ACCESS_AUD pin Cloudflare Access. Run as `make signin-reset`, which restarts the site."""

import os

import psycopg

KEYS = ("enabled", "cf_team", "cf_aud")


def reset(conn) -> int:
    cur = conn.execute("DELETE FROM cc_auth WHERE key = ANY(%s)", (list(KEYS),))
    conn.commit()
    return cur.rowcount


if __name__ == "__main__":
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as conn:
        print(f"cleared {reset(conn)} setting(s); the password is kept")

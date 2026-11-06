"""Lost the phone: forget every passkey and the command PIN. Shell access to the host is the proof of ownership.

The password and sign-in are kept. Tier 3 commands then need the password again, or a new passkey or PIN added
in Settings › Access (the first one needs no step-up when nothing else confirms commands). Run as
`make passkeys-reset`, which restarts the site."""

import os

import psycopg

import passkeys

KEYS = ("command_pin", "passkey_user")


def reset(conn) -> tuple[int, int]:
    conn.execute(passkeys.DDL)
    gone = conn.execute("DELETE FROM cc_passkeys").rowcount
    pin = conn.execute("DELETE FROM cc_auth WHERE key = ANY(%s)", (list(KEYS),)).rowcount
    conn.commit()
    return gone, pin


if __name__ == "__main__":
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as conn:
        keys, pin = reset(conn)
        print(f"removed {keys} passkey(s){' and the command PIN' if pin else ''}; the password is kept")

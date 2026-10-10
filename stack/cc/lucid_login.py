"""Mint the Lucid refresh token once, interactively, and write it where the connector reads it.

    make lucid-login          # asks for the Lucid username and password, writes stack/secrets/lucid-token.json

The password is used for one login and never stored; the refresh token does not rotate,
so the file lasts until the account's password changes or the token is revoked in the app.
"""

import asyncio
import getpass
import json
import os
import sys


async def mint(username: str, password: str) -> str:
    from lucidmotors import LucidAPI  # the connector's dependency; only available inside the cc image

    async with LucidAPI() as api:
        await api.login(username, password)
        token = api.refresh_token
        if not token:
            raise SystemExit("Lucid answered, but no refresh token came back.")
        return token


def main(argv: list[str]) -> int:
    out = argv[1] if len(argv) > 1 else "/out/lucid-token.json"
    username = os.environ.get("LUCID_USERNAME") or input("Lucid username (e-mail): ").strip()
    password = os.environ.get("LUCID_PASSWORD") or getpass.getpass("Lucid password (used once, not stored): ")
    token = asyncio.run(mint(username, password))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"refresh_token": token}, fh)
    os.chmod(out, 0o600)
    print(f"wrote {out} (mode 600). Restart the connector: docker compose -f stack/docker-compose.yml up -d --no-deps cc")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

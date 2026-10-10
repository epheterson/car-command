"""Who runs what in the connector container, and what each can see.

The supervisor runs as root, for one reason: to read the key and passwords the entrypoint
put in root-only files, open the sealed sources with the key, and write CarConnectivity's
config for it. Everything that runs code someone else wrote — CarConnectivity with its
connectors and plugins, pip, the import check of a new connector — runs as the
unprivileged user `carcommand` with a short, explicit environment: no CC_SECRET_KEY, no
database or web passwords (the database plugin's URL is in its 0600 config file, which it
needs), nothing from the container's environment that is not on the list below. Root's
own Python never reads the plugins volume (PYTHONNOUSERSITE, python3 -s), so nothing
installed there can run as root.
"""

from __future__ import annotations

import os
import subprocess

RUN_DIR = os.environ.get("CC_RUN_DIR") or "/run/carcommand"
USER = "carcommand"
# What a child is handed from the supervisor's environment, and nothing else.
KEEP = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONUSERBASE", "CC_GEOCODER_URL")
# What the entrypoint moves out of the environment into root-only files.
SECRETS = ("CC_SECRET_KEY", "DB_PASSWORD", "WEBUI_PASSWORD", "REST_PASSWORD", "CC_CONNECTORS_JSON")


def ids() -> tuple[int, int]:
    try:
        import pwd

        p = pwd.getpwnam(USER)
        return p.pw_uid, p.pw_gid
    except (ImportError, KeyError):
        return 10001, 10001


def child_env(env) -> dict:
    out = {k: env[k] for k in KEEP if k in env}
    out["HOME"] = "/tmp"
    return out


def drop() -> dict:
    """Popen/run arguments that make the child the unprivileged user, when we are root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return {}
    uid, gid = ids()
    return {"user": uid, "group": gid, "extra_groups": [], "umask": 0o077}


def run(cmd, **kw):
    """subprocess.run as the unprivileged user; the caller passes the (plugin) environment."""
    return subprocess.run(cmd, **drop(), **kw)


def spawn(config_path: str):
    """CarConnectivity, as the unprivileged user, with the plugin environment as it is now
    (the supervisor sets CC_GEOCODER_URL just before)."""
    return subprocess.Popen(["carconnectivity", config_path], env=child_env(os.environ), cwd="/tmp", **drop())


def hand_over(path: str) -> None:
    """A file root wrote for the child: the child's own, 0600."""
    os.chmod(path, 0o600)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(path, *ids())


def load_secrets(env: dict, directory: str | None = None) -> dict:
    """The values the entrypoint moved into root-only files, back into this process's env."""
    directory = directory or os.path.join(RUN_DIR, "secrets")
    for name in SECRETS:
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                env[name] = fh.read()
        except OSError:
            continue
    return env

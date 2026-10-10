"""Other brands' CarConnectivity connectors, installed while the container runs.

The image carries the connectors CC_CONNECTORS named at build time. A brand added from
Settings › Cars and sources arrives as a cc_sources row naming a catalogue entry; this
installs it with `pip install --user` into PYTHONUSERBASE (/plugins, a named volume, so it
survives restarts and rebuilds). CarConnectivity finds connectors by walking the
carconnectivity_connectors namespace, which spans the image's packages and the user site.

Kept safe by what it refuses rather than by trust:
  - only an entry of connectors.json, the catalogue this image ships (the site's copy is
    the same file), matched exactly: package, release, connector type and id. A row that
    names anything else is refused before pip or an import is ever run;
  - every wheel the entry adds is pinned by sha256 (--require-hashes --no-deps -r lock),
    hashes checked against PyPI's when scripts/connector-lock.py wrote them; wheels only,
    so nothing runs at install time;
  - every distribution already present is a constraint, so an install can add packages but
    never change one the image (or another plugin) runs on;
  - the connector must import in a fresh interpreter before it goes into the config;
  - a package the image carries is never installed over or removed.
pip and the import check run as the unprivileged user with the plugin environment
(supervise.child_env): no key, no password, nothing but package names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

CATALOGUE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectors.json")
SHA256 = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?")
PIP_TIMEOUT_S = 300
_cache: dict = {}


def catalogue(path: str = CATALOGUE) -> dict:
    """key -> entry, from the catalogue file this image ships. Empty when it is missing."""
    if path not in _cache:
        try:
            with open(path, encoding="utf-8") as fh:
                _cache[path] = json.load(fh).get("connectors") or {}
        except (OSError, ValueError):
            _cache[path] = {}
    return _cache[path]


def entry_for(config: dict, cat: dict | None = None) -> tuple[dict | None, str | None]:
    """The catalogue entry a row names, or why the row is refused."""
    cat = catalogue() if cat is None else cat
    key = config.get("connector")
    entry = cat.get(key) if isinstance(key, str) else None
    if entry is None:
        return None, "not a connector this image's catalogue offers"
    for field in ("package", "version", "type", "connector_id"):
        if config.get(field) != entry.get(field):
            return None, f"its {field.replace('_', ' ')} is not the catalogue's for {key}"
    reqs = entry.get("requirements") or []
    if not reqs or _norm(reqs[0].get("name", "")) != _norm(entry["package"]) or reqs[0].get("version") != entry["version"]:
        return None, f"the catalogue has no pinned wheel for {key}"
    for r in reqs:
        if not (NAME.fullmatch(str(r.get("name"))) and VERSION.fullmatch(str(r.get("version"))) and SHA256.fullmatch(str(r.get("sha256")))):
            return None, f"the catalogue's pins for {key} are malformed"
    return entry, None


def upgrade_to(config: dict, cat: dict | None = None) -> str | None:
    """The catalogue's release, for a row naming the same package, type and id at another one
    (the site's copy of the catalogue was older or newer than this image's). None otherwise:
    a row that differs in anything else is refused, not upgraded."""
    cat = catalogue() if cat is None else cat
    entry = cat.get(config.get("connector")) if isinstance(config.get("connector"), str) else None
    if entry is None or config.get("version") == entry.get("version"):
        return None
    if any(config.get(f) != entry.get(f) for f in ("package", "type", "connector_id")):
        return None
    return entry["version"] if problem({**config, "version": entry["version"]}, cat) is None else None


def problem(config: dict, cat: dict | None = None) -> str | None:
    """Why a row's request cannot be installed or run as it stands, or None."""
    return entry_for(config, cat)[1]


def lock_text(entry: dict) -> str:
    return "".join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in entry["requirements"])


def user_site(env) -> str:
    """Where pip --user puts packages for this interpreter (lib/python3.12/site-packages on Linux)."""
    import sysconfig

    base = env.get("PYTHONUSERBASE") or os.path.expanduser("~/.local")
    return sysconfig.get_path("purelib", sysconfig.get_preferred_scheme("user"), vars={"userbase": base})


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def present(env, paths: list[str] | None = None) -> dict[str, tuple[str, bool]]:
    """Every distribution Python can see, name -> (version, whether it is in the user site).
    Reads metadata files only; nothing from the user site is imported."""
    from importlib import metadata

    usite = user_site(env)
    out: dict[str, tuple[str, bool]] = {}
    for where in [usite] + list(paths if paths is not None else sys.path):
        if not where or not os.path.isdir(where):
            continue
        for d in metadata.distributions(path=[where]):
            name = d.metadata["Name"]
            if name and _norm(name) not in out:
                out[_norm(name)] = (d.version, where == usite)
    return out


def constraints(have: dict[str, tuple[str, bool]], package: str) -> str:
    """Pin everything installed, except the package being put in (it may be an upgrade)."""
    return "".join(f"{n}=={v}\n" for n, (v, _user) in sorted(have.items()) if n != _norm(package))


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")]
    return ((errors or lines or ["no output"])[0])[:240]


def _write(path: str, text: str) -> None:
    """Root's, readable by the plugin user's group (pip runs as that user) and nobody else;
    never through a link someone planted."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            import privsep

            os.fchown(fh.fileno(), 0, privsep.ids()[1])
        os.fchmod(fh.fileno(), 0o640)
        fh.write(text)


def install(config: dict, env, run=subprocess.run, have=None, tmpdir: str = "/tmp", cat: dict | None = None) -> str | None:
    """Make the row's connector importable. None when it is; otherwise what went wrong, in a
    sentence the page can show. A refused row runs nothing at all."""
    entry, why = entry_for(config, cat)
    if why:
        return f"This request cannot be installed: {why}."
    package, version, ctype = entry["package"], entry["version"], entry["type"]
    have = present(env) if have is None else have
    got = have.get(_norm(package))
    if got and (not got[1] or got[0] == version):
        # The image's own copy is used as it is (CC_CONNECTORS, the advanced way); a plugin at the release asked for is already in.
        return None if _imports(ctype, env, run) else f"{package} is installed but its connector does not load. Rebuild the image or remove it and add it again."
    cpath, lpath = os.path.join(tmpdir, "cc-plugin-constraints.txt"), os.path.join(tmpdir, "cc-plugin-lock.txt")
    _write(cpath, constraints(have, package))
    _write(lpath, lock_text(entry))
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--require-hashes", "--no-deps", "--only-binary", ":all:"]
    cmd += ["--no-cache-dir", "--disable-pip-version-check", "--no-input", "--constraint", cpath, "--requirement", lpath]
    try:
        done = run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S, env=dict(env))
    except subprocess.TimeoutExpired:
        return f"Installing {package} {version} took longer than {PIP_TIMEOUT_S // 60} minutes. Is PyPI reachable from the cc container?"
    if done.returncode != 0:
        err = done.stderr or done.stdout or ""
        detail = _tail(err)
        if "conflict" in err.lower() or "ResolutionImpossible" in err:
            return f"{package} {version} needs packages at releases other than the ones already installed (the image's, or another brand's), so it was not installed. Build it into the image instead: CC_CONNECTORS in .env (see docs/sources.md)."
        if "THESE PACKAGES DO NOT MATCH THE HASHES" in err:
            return f"A wheel for {package} {version} did not match the sha256 in the catalogue, so nothing was installed."
        return f"pip could not install {package} {version}: {detail}"
    if not _imports(ctype, env, run):
        uninstall(config, env, run, have={_norm(package): (version, True)}, cat=cat)  # just put in the user site by the line above
        return f"{package} {version} installed but its connector did not load, so it was taken out again."
    return None


def _imports(ctype: str, env, run) -> bool:
    try:
        done = run([sys.executable, "-c", f"import carconnectivity_connectors.{ctype}.connector"], capture_output=True, text=True, timeout=120, env=dict(env))
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def uninstall(config: dict, env, run=subprocess.run, have=None, cat: dict | None = None) -> str | None:
    """Take a plugin out of the user site, by package name: any release of a package the
    catalogue names (one installed from an older catalogue too). A package the image carries
    is left alone, and anything the catalogue does not name runs nothing."""
    cat = catalogue() if cat is None else cat
    if not any(isinstance(e, dict) and e.get("package") == config.get("package") for e in cat.values()):
        return None
    have = present(env) if have is None else have
    got = have.get(_norm(config["package"]))
    if not got or not got[1]:
        return None
    done = run([sys.executable, "-m", "pip", "uninstall", "--yes", "--disable-pip-version-check", config["package"]], capture_output=True, text=True, timeout=PIP_TIMEOUT_S, env=dict(env))
    return None if done.returncode == 0 else _tail(done.stderr or done.stdout)


def installed(config: dict, env, have=None) -> bool:
    """Whether the row's connector is there to run, by metadata alone (no import, no pip):
    cheap enough for the start, before anything slower."""
    have = present(env) if have is None else have
    got = have.get(_norm(config.get("package") or ""))
    return bool(got) and (not got[1] or got[0] == config.get("version"))


def block(config: dict, values: dict) -> dict:
    """The connector's entry in carconnectivity.json. Warnings only: at info, connectors log
    their config, and the masking they do leaves the user name in."""
    out = {"type": config["type"], "config": {**values, "log_level": "warning"}}
    if config.get("connector_id"):
        out["connector_id"] = config["connector_id"]
    return out

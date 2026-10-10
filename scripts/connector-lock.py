#!/usr/bin/env python3
"""Pin every other-brand connector, and everything it adds to the image, by sha256.

    make connector-lock

Runs inside the connector image (the Makefile target builds it and mounts the repo), because
"what a connector adds" means "beyond the packages this image already runs on". For each
entry in stack/shared/connectors.json it asks pip which wheels a plain install would add
(--dry-run --report, wheels only, everything the image has pinned as a constraint), checks
each wheel's sha256 against the digest PyPI publishes for that exact file, and writes the
list back as the entry's `requirements`. An entry is refused (the script stops, nothing is
written) when a wheel is not pure Python, PyPI's digest differs, or pip cannot resolve it
beside the image. Then it copies the file into stack/cc/ and stack/web/, the two build
contexts, so both images ship the same bytes.

Input fields per entry, kept by hand: package, version, type, connector_id (optional).
Output field, written here: requirements = [{name, version, file, sha256}], the connector first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "stack" / "shared" / "connectors.json"
COPIES = (ROOT / "stack" / "cc" / "connectors.json", ROOT / "stack" / "web" / "connectors.json")


def norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def image_packages() -> dict[str, str]:
    """name -> version for the image's own site-packages (the user site is never counted)."""
    out = {}
    for path in sys.path:
        if not path or "site-packages" not in path or path.startswith(os.environ.get("PYTHONUSERBASE", "\0")):
            continue
        for d in metadata.distributions(path=[path]):
            out.setdefault(norm(d.metadata["Name"]), d.version)
    return out


def pypi_sha256(name: str, version: str, filename: str) -> str:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as resp:
        files = json.load(resp)["urls"]
    for f in files:
        if f["filename"] == filename:
            if f.get("yanked"):
                raise SystemExit(f"{filename} is yanked on PyPI")
            return f["digests"]["sha256"]
    raise SystemExit(f"{filename} is not among PyPI's files for {name} {version}")


def lock(entry: dict, image: dict[str, str]) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        cons, report = Path(tmp) / "constraints.txt", Path(tmp) / "report.json"
        cons.write_text("".join(f"{n}=={v}\n" for n, v in sorted(image.items()) if n != norm(entry["package"])))
        env = dict(os.environ, PYTHONNOUSERSITE="1")
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--ignore-installed", "--no-cache-dir", "--disable-pip-version-check"]
        cmd += ["--only-binary", ":all:", "--report", str(report), "-c", str(cons), f"{entry['package']}=={entry['version']}"]
        done = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if done.returncode:
            raise SystemExit(f"{entry['package']} {entry['version']} does not resolve beside the image:\n{done.stderr.strip()[-600:]}")
        items = json.loads(report.read_text())["install"]
    reqs = []
    for item in items:
        name, version = item["metadata"]["name"], item["metadata"]["version"]
        if image.get(norm(name)) == version:
            continue  # the image already runs exactly this
        url = item["download_info"]["url"]
        filename = url.rsplit("/", 1)[1]
        if not filename.endswith("-none-any.whl"):
            raise SystemExit(f"{filename} is not a pure-Python wheel; it cannot be pinned for every platform the image builds on")
        got = item["download_info"].get("archive_info", {}).get("hashes", {}).get("sha256") or item["download_info"].get("archive_info", {}).get("hash", "").removeprefix("sha256=")
        published = pypi_sha256(name, version, filename)
        if got and got != published:
            raise SystemExit(f"{filename}: pip saw sha256 {got}, PyPI publishes {published}")
        reqs.append({"name": name, "version": version, "file": filename, "sha256": published})
    reqs.sort(key=lambda r: (norm(r["name"]) != norm(entry["package"]), norm(r["name"])))
    if not reqs or norm(reqs[0]["name"]) != norm(entry["package"]) or reqs[0]["version"] != entry["version"]:
        raise SystemExit(f"{entry['package']} {entry['version']} itself is not in what pip would install")
    return reqs


def dump(data: dict) -> str:
    """JSON with one line per entry and one per wheel: short, and a re-pin diffs by the line."""

    def one(v) -> str:
        return json.dumps(v, ensure_ascii=False)

    out = ["{", f'  "about": {one(data.get("about", ""))},', f'  "python": {one(data.get("python", ""))},', '  "connectors": {']
    keys = list(data["connectors"])
    for i, key in enumerate(keys):
        entry = dict(data["connectors"][key])
        reqs = entry.pop("requirements", [])
        out.append(f"    {one(key)}: {one(entry)[:-1]}, " + '"requirements": [')
        out += [f"      {one(r)}" + ("," if j < len(reqs) - 1 else "") for j, r in enumerate(reqs)]
        out.append("    ]}" + ("," if i < len(keys) - 1 else ""))
    out += ["  }", "}"]
    return "\n".join(out) + "\n"


def main() -> int:
    data = json.loads(SHARED.read_text())
    image = image_packages()
    for key, entry in data["connectors"].items():
        entry["requirements"] = lock(entry, image)
        print(f"{key}: " + ", ".join(f"{r['name']} {r['version']}" for r in entry["requirements"]))
    data["python"] = f"{sys.version_info.major}.{sys.version_info.minor}"
    SHARED.write_text(dump(data))
    for copy in COPIES:
        shutil.copyfile(SHARED, copy)
    print(f"wrote {SHARED.relative_to(ROOT)} and its copies in {', '.join(str(c.parent.relative_to(ROOT)) for c in COPIES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

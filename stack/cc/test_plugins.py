"""Other brands' connectors installed at runtime: only a catalogue entry, only by hash, never over the image."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import plugins  # noqa: E402

H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64
CAT = {
    "volkswagen": {
        "package": "carconnectivity-connector-volkswagen",
        "version": "0.10.6",
        "type": "volkswagen",
        "requirements": [
            {"name": "carconnectivity-connector-volkswagen", "version": "0.10.6", "file": "x.whl", "sha256": H1},
            {"name": "oauthlib", "version": "3.3.1", "file": "o.whl", "sha256": H2},
            {"name": "PyJWT", "version": "2.15.1", "file": "j.whl", "sha256": H3},
        ],
    },
    "cupra": {
        "package": "carconnectivity-connector-seatcupra",
        "version": "0.6.4",
        "type": "seatcupra",
        "connector_id": "cupra",
        "requirements": [{"name": "carconnectivity-connector-seatcupra", "version": "0.6.4", "file": "s.whl", "sha256": H1}],
    },
}
VW = {"connector": "volkswagen", "package": "carconnectivity-connector-volkswagen", "version": "0.10.6", "type": "volkswagen", "state": "install"}
IMAGE = {"carconnectivity": ("0.11.11", False), "requests": ("2.34.2", False)}


class Pip:
    """subprocess.run as pip and python would answer it, recording every command."""

    def __init__(self, install_rc=0, import_rc=0, stderr=""):
        self.calls, self.install_rc, self.import_rc, self.stderr = [], install_rc, import_rc, stderr

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[1:3] == ["-m", "pip"]:
            return subprocess.CompletedProcess(cmd, self.install_rc if cmd[3] == "install" else 0, "", self.stderr)
        return subprocess.CompletedProcess(cmd, self.import_rc, "", "")


def test_a_row_that_is_not_exactly_a_catalogue_entry_is_refused_before_anything_runs(tmp_path):
    assert plugins.problem(VW, CAT) is None
    assert plugins.problem(dict(VW, connector="cupra", package="carconnectivity-connector-seatcupra", version="0.6.4", type="seatcupra", connector_id="cupra"), CAT) is None
    refused = [
        dict(VW, connector="evil"),  # not an entry
        dict(VW, package="carconnectivity-connector-evil"),  # an entry's key, another package
        dict(VW, version="0.10.7"),  # another release
        dict(VW, type="lucid"),  # another connector type
        dict(VW, connector_id="vw2"),  # an id the entry does not have
        {k: v for k, v in VW.items() if k != "connector"},
    ]
    for row in refused:
        pip = Pip()
        assert plugins.problem(row, CAT), row
        assert "cannot be installed" in plugins.install(row, {}, run=pip, have=IMAGE, tmpdir=str(tmp_path), cat=CAT)
        assert plugins.uninstall(row, {}, run=pip, have={"carconnectivity-connector-evil": ("1", True)}, cat=CAT) is None
        assert pip.calls == [], f"nothing installed or imported for {row}"
    unpinned = {"volkswagen": {**CAT["volkswagen"], "requirements": [{"name": "carconnectivity-connector-volkswagen", "version": "0.10.6", "sha256": "nothex"}]}}
    assert "malformed" in plugins.problem(VW, unpinned)
    assert "no pinned wheel" in plugins.problem(VW, {"volkswagen": {**CAT["volkswagen"], "requirements": []}})


def test_install_is_hashes_only_no_deps_wheels_only_into_the_user_site(tmp_path):
    pip = Pip()
    assert plugins.install(VW, {"PYTHONUSERBASE": "/plugins"}, run=pip, have=IMAGE, tmpdir=str(tmp_path), cat=CAT) is None
    cmd, kw = pip.calls[0]
    assert cmd[1:4] == ["-m", "pip", "install"] and "--user" in cmd and "--require-hashes" in cmd and "--no-deps" in cmd
    assert cmd[cmd.index("--only-binary") + 1] == ":all:" and kw["env"] == {"PYTHONUSERBASE": "/plugins"} and kw["timeout"]
    lock = (tmp_path / "cc-plugin-lock.txt").read_text()
    assert lock == (f"carconnectivity-connector-volkswagen==0.10.6 --hash=sha256:{H1}\noauthlib==3.3.1 --hash=sha256:{H2}\nPyJWT==2.15.1 --hash=sha256:{H3}\n")
    assert cmd[cmd.index("--requirement") + 1] == str(tmp_path / "cc-plugin-lock.txt") and not any(a.startswith("carconnectivity-connector") for a in cmd), "only the lock names packages"
    pins = (tmp_path / "cc-plugin-constraints.txt").read_text()
    assert pins == "carconnectivity==0.11.11\nrequests==2.34.2\n", "what the image runs on cannot move"
    assert oct((tmp_path / "cc-plugin-lock.txt").stat().st_mode & 0o777) == "0o640", "readable by pip's group, which runs as another user; by nobody else"
    assert pip.calls[1][0][1:] == ["-c", "import carconnectivity_connectors.volkswagen.connector"], "it must load before it goes in the config"


def test_the_image_copy_is_used_as_it_is_and_a_present_plugin_is_not_reinstalled(tmp_path):
    for have in ({**IMAGE, "carconnectivity-connector-volkswagen": ("0.9.3", False)}, {**IMAGE, "carconnectivity-connector-volkswagen": ("0.10.6", True)}):
        pip = Pip()
        assert plugins.install(VW, {}, run=pip, have=have, tmpdir=str(tmp_path), cat=CAT) is None
        assert [c[0][1] for c in pip.calls] == ["-c"], "only the import check"
        assert plugins.installed(VW, {}, have=have)
    assert not plugins.installed(VW, {}, have=IMAGE)
    pip = Pip()  # an older plugin of ours: upgraded, and not pinned against itself
    assert plugins.install(VW, {}, run=pip, have={**IMAGE, "carconnectivity-connector-volkswagen": ("0.9.3", True)}, tmpdir=str(tmp_path), cat=CAT) is None
    assert pip.calls[0][0][3] == "install" and "volkswagen" not in (tmp_path / "cc-plugin-constraints.txt").read_text()


def test_a_conflict_or_a_bad_hash_says_so_and_a_connector_that_will_not_load_is_taken_out(tmp_path):
    pip = Pip(install_rc=1, stderr="ERROR: Cannot install x because these package versions have conflicting dependencies.\nResolutionImpossible")
    err = plugins.install(VW, {}, run=pip, have=IMAGE, tmpdir=str(tmp_path), cat=CAT)
    assert "CC_CONNECTORS" in err and "not installed" in err
    pip = Pip(install_rc=1, stderr="ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.")
    assert "did not match the sha256" in plugins.install(VW, {}, run=pip, have=IMAGE, tmpdir=str(tmp_path), cat=CAT)
    pip = Pip(import_rc=1)
    have = dict(IMAGE)

    def run(cmd, **kw):
        if cmd[3:4] == ["install"]:
            have["carconnectivity-connector-volkswagen"] = ("0.10.6", True)  # pip put it in
        return pip(cmd, **kw)

    err = plugins.install(VW, {}, run=run, have=have, tmpdir=str(tmp_path), cat=CAT)
    assert "did not load" in err and pip.calls[-1][0][3:5] == ["uninstall", "--yes"]


def test_a_timeout_is_a_sentence(tmp_path):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    assert "longer than" in plugins.install(VW, {}, run=slow, have=IMAGE, tmpdir=str(tmp_path), cat=CAT)


def test_a_planted_link_is_not_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (tmp_path / "cc-plugin-lock.txt").symlink_to(target)
    try:
        plugins.install(VW, {}, run=Pip(), have=IMAGE, tmpdir=str(tmp_path), cat=CAT)
    except OSError:
        pass
    assert target.read_text() == "keep"


def test_uninstall_touches_only_our_own_plugins():
    pip = Pip()
    assert plugins.uninstall(VW, {}, run=pip, have={**IMAGE, "carconnectivity-connector-volkswagen": ("0.10.6", False)}, cat=CAT) is None and pip.calls == [], "the image's copy stays"
    assert plugins.uninstall(VW, {}, run=pip, have=IMAGE, cat=CAT) is None and pip.calls == [], "nothing there, nothing to do"
    assert plugins.uninstall(VW, {}, run=pip, have={**IMAGE, "carconnectivity-connector-volkswagen": ("0.10.6", True)}, cat=CAT) is None
    assert pip.calls[0][0][3:] == ["uninstall", "--yes", "--disable-pip-version-check", "carconnectivity-connector-volkswagen"]


def test_present_finds_the_user_site_first(tmp_path):
    base = tmp_path / "plugins"
    site = Path(plugins.user_site({"PYTHONUSERBASE": str(base)}))
    (site / "carconnectivity_connector_volkswagen-0.10.6.dist-info").mkdir(parents=True)
    (site / "carconnectivity_connector_volkswagen-0.10.6.dist-info" / "METADATA").write_text("Name: carconnectivity-connector-volkswagen\nVersion: 0.10.6\n")
    other = tmp_path / "image"
    (other / "carconnectivity_connector_volkswagen-0.9.3.dist-info").mkdir(parents=True)
    (other / "carconnectivity_connector_volkswagen-0.9.3.dist-info" / "METADATA").write_text("Name: carconnectivity-connector-volkswagen\nVersion: 0.9.3\n")
    assert plugins.present({"PYTHONUSERBASE": str(base)}, [str(other)])["carconnectivity-connector-volkswagen"] == ("0.10.6", True)


def test_the_block_logs_warnings_only_and_names_its_id():
    b = plugins.block(dict(VW, type="seatcupra", connector_id="cupra"), {"username": "u@example.com", "password": "pw", "brand": "cupra"})
    assert b == {"type": "seatcupra", "connector_id": "cupra", "config": {"username": "u@example.com", "password": "pw", "brand": "cupra", "log_level": "warning"}}
    assert "connector_id" not in plugins.block(VW, {})


def test_the_shipped_catalogue_is_the_shared_one_and_every_entry_is_pinned():
    shipped = (HERE / "connectors.json").read_bytes()
    shared = HERE.parent / "shared" / "connectors.json"
    assert shipped == shared.read_bytes(), "run make connector-lock: stack/cc/connectors.json must be stack/shared/connectors.json"
    cat = json.loads(shipped)["connectors"]
    assert cat and plugins.catalogue() == cat
    for key, entry in cat.items():
        row = {"connector": key, **{k: entry.get(k) for k in ("package", "version", "type", "connector_id")}}
        assert plugins.problem(row) is None, key
        assert all(r["file"].endswith("-none-any.whl") for r in entry["requirements"]), key


def test_the_image_runs_plugins_as_the_user_on_a_volume_it_owns():
    docker = (HERE / "Dockerfile").read_text()
    assert "ENV PYTHONUSERBASE=/plugins PYTHONNOUSERSITE=1" in docker and "chown carcommand:carcommand /plugins" in docker
    copied = next(line for line in docker.splitlines() if line.startswith("COPY render_config.py"))
    assert all(f in copied.split() for f in ("plugins.py", "privsep.py", "connectors.json"))
    assert '"python3", "-s", "/app/healthcheck.py"' in docker, "root's healthcheck never reads the plugins volume"
    compose = (HERE.parent / "docker-compose.yml").read_text()
    cc = compose[compose.index("  cc:") : compose.index("  schema:")]
    assert "cc_plugins:/plugins" in cc and "\n  cc_plugins:\n" in compose
    schema = compose[compose.index("  schema:") : compose.index("  web:")]
    assert 'user: "10001:10001"' in schema

"""History import from the browser: the file checks, the job's states, a re-run, and each state drawn."""

import gzip
import json
from datetime import datetime, timezone

import pytest

import importjob
from importjob import Skip, Store, clean_name, validate_teslafi
from test_empty import bare, client  # noqa: F401 - fixtures


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    """A small synthetic TeslaFi export from the demo generator, plain and gzipped."""
    import demo

    d = tmp_path_factory.mktemp("exports")
    plain, gz = d / "Alpha.csv", d / "Alpha.csv.gz"
    demo.write(str(plain), days=3)
    demo.write(str(gz), days=3)
    return plain, gz


def upload(store, name, data: bytes):
    up, why = store.begin(name, len(data))
    assert why is None, why
    for i in range(0, len(data), 4096):
        assert up.write(data[i : i + 4096]) is None
    return up.finish()


# ---- names and format checks
@pytest.mark.parametrize(
    "raw,ok", [("TeslaFiData.csv.gz", True), ("a b-1.csv", True), ("x.gz", True), ("../../etc/passwd", False), ("notes.txt", False), ("", False), (None, False), (".csv", False), ("a\x00b.csv", False)]
)
def test_only_export_looking_names_are_kept_and_never_a_path(raw, ok):
    assert (clean_name(raw) is not None) is ok


def test_a_path_in_the_name_is_cut_to_the_file(tmp_path):
    assert clean_name("C:\\Users\\x\\Alpha.csv") == "Alpha.csv" and clean_name("/tmp/../Alpha.csv.gz") == "Alpha.csv.gz"


def test_a_demo_export_passes_plain_and_gzipped(export):
    for f in export:
        assert validate_teslafi(f) == (True, "")


@pytest.mark.parametrize(
    "body,why",
    [
        (b"", "No data rows"),
        (b"just,some,columns\n1,2,3\n", "Not a TeslaFi export"),
        (b"Date,battery_level\n", "No data rows"),
        (b"Date,battery_level\nnot a date,50\nalso not,51\n", "No row with a date"),
        (b"\x00\x01\x02binary\n\x00\x00\n", "Not a"),
    ],
)
def test_a_file_that_is_not_an_export_is_turned_away_with_a_reason(tmp_path, body, why):
    f = tmp_path / "x.csv"
    f.write_bytes(body)
    ok, got = validate_teslafi(f)
    assert not ok and why in got


def test_a_broken_gzip_is_unreadable_not_a_crash(tmp_path):
    f = tmp_path / "x.csv.gz"
    f.write_bytes(b"\x1f\x8b\x08\x00garbage")
    assert validate_teslafi(f)[0] is False


def test_only_the_head_of_a_gzip_is_read(tmp_path):
    f = tmp_path / "bomb.csv.gz"
    with gzip.open(f, "wb") as fh:
        fh.write(b"Date,battery_level\n2026-01-01 10:00:00,50\n" + b"2026-01-01 10:00:01,50\n" * 2_000_000)
    assert validate_teslafi(f) == (True, "")


# ---- parsing with progress
def test_parsing_reports_how_far_through_the_file_it_is(export, monkeypatch):
    import teslafi_load

    monkeypatch.setattr(teslafi_load, "PROGRESS_ROWS", 200)
    seen = []
    for f in export:
        seen.clear()
        out = teslafi_load.parse(f, lambda read, total, at: seen.append((read, total, at)))
        assert out["rows"] > 200 and out["trips"] and seen
        assert all(0 < r <= t for r, t, _ in seen) and [s[0] for s in seen] == sorted(s[0] for s in seen)
        assert seen[-1][2] <= out["last"]


def test_plain_and_gzipped_copies_of_one_export_replay_the_same(export):
    import teslafi_load

    a, b = (teslafi_load.parse(f) for f in export)
    assert (a["rows"], len(a["trips"]), len(a["charging_sessions"])) == (b["rows"], len(b["trips"]), len(b["charging_sessions"]))


# ---- staging
def test_an_upload_is_checked_then_kept_and_the_same_name_replaces_it(tmp_path, export):
    s = Store(tmp_path)
    body = export[0].read_bytes()
    assert upload(s, "Alpha.csv", body) is None and upload(s, "Alpha.csv", body) is None
    assert [f["name"] for f in s.view()["staged"]] == ["Alpha.csv"]
    assert (s.staged_dir / "Alpha.csv").read_bytes() == body and not list(s.staged_dir.glob("*.part"))


def test_a_wrong_file_leaves_nothing_behind(tmp_path):
    s = Store(tmp_path)
    assert "Not a TeslaFi export" in upload(s, "x.csv", b"a,b\n1,2\n")
    assert s.view()["staged"] == [] and list(s.staged_dir.iterdir()) == []


def test_the_size_cap_stops_a_file_mid_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(importjob, "MAX_FILE", 10_000)
    s = Store(tmp_path)
    up, why = s.begin("big.csv", None)
    assert why is None
    assert up.write(b"x" * 9_000) is None and "limit" in up.write(b"x" * 2_000)
    up.abort()
    assert list(s.staged_dir.iterdir()) == []
    assert "limit" in s.begin("big.csv", 10_001)[1], "a declared length over the cap is refused before a byte is written"


def test_the_number_of_waiting_files_is_capped(tmp_path, export):
    s = Store(tmp_path)
    for i in range(importjob.MAX_FILES):
        assert upload(s, f"c{i}.csv", export[0].read_bytes()) is None
    assert "At most" in s.begin("one-more.csv", 1)[1]


def test_unstaging_removes_the_file(tmp_path, export):
    s = Store(tmp_path)
    upload(s, "a.csv", export[0].read_bytes())
    upload(s, "b.csv", export[0].read_bytes())
    s.unstage("a.csv")
    assert [f["name"] for f in s.view()["staged"]] == ["b.csv"] and not (s.staged_dir / "a.csv").exists()
    s.unstage()
    assert s.view()["staged"] == []


# ---- the job's states
def fake_loader(calls=None, bad=()):
    def load(path, progress):
        if calls is not None:
            calls.append(path.name)
        if path.name in bad:
            raise Skip("No VIN in the file.")
        progress(50, f"Reading {path.name}: through Sep 2026")
        return {"drives": 10, "charges": 4, "days": 30, "positions": 100, "detail": "Alpha, Sep 2026 to Oct 2026"}

    return load


def staged_store(tmp_path, export, names=("a.csv", "b.csv"), **kw):
    s = Store(tmp_path, threaded=False, **kw)
    for n in names:
        assert upload(s, n, export[0].read_bytes()) is None
    return s


def test_a_job_runs_every_file_and_adds_up_the_result(tmp_path, export):
    named = []
    s = staged_store(tmp_path, export, loader=fake_loader(), after=lambda: named.append(1) or 3)
    assert s.start_teslafi() is None
    job = s.view()["job"]
    assert job["state"] == "done" and job["pct"] == 100 and not s.view()["busy"]
    assert job["result"] == {"drives": 20, "new_drives": 20, "charges": 8, "days": 60, "positions": 200} and job["places"] == 3 and named == [1]
    assert [i["state"] for i in job["items"]] == ["done", "done"] and not list(s.job_dir.iterdir()), "a loaded upload is not kept"
    assert s.view()["staged"] == []


def test_a_file_that_cannot_load_is_skipped_with_its_reason_and_the_rest_go_on(tmp_path, export):
    s = staged_store(tmp_path, export, loader=fake_loader(bad=("a.csv",)))
    s.start_teslafi()
    job = s.view()["job"]
    assert job["state"] == "done"
    assert [(i["name"], i["state"], i["detail"]) for i in job["items"]][0] == ("a.csv", "skipped", "No VIN in the file.")
    assert job["result"]["drives"] == 10


def test_a_crash_in_a_loader_skips_that_file_without_leaking_its_message(tmp_path, export):
    def boom(path, progress):
        raise RuntimeError("password=hunter2 in /home/someone/file")

    s = staged_store(tmp_path, export, names=("a.csv",), loader=boom)
    s.start_teslafi()
    job = s.view()["job"]
    assert job["state"] == "failed" and "RuntimeError" not in job["items"][0]["detail"] and "unexpected error" in job["items"][0]["detail"] and "hunter2" not in json.dumps(s.view())


def test_nothing_loaded_is_a_failed_job_you_can_start_over(tmp_path, export):
    calls = []
    s = staged_store(tmp_path, export, names=("a.csv",), loader=fake_loader(calls, bad=("a.csv",)))
    s.start_teslafi()
    assert s.view()["job"]["state"] == "failed" and s.view()["job"]["error"].startswith("Nothing was imported")
    s.loader = fake_loader(calls)
    assert s.resume(restart=True) is None and s.view()["job"]["state"] == "done" and calls == ["a.csv", "a.csv"]


def test_only_one_job_runs_at_a_time(tmp_path, export):
    s = staged_store(tmp_path, export, loader=fake_loader())
    s.state["job"] = {"id": "x", "kind": "teslafi", "state": "running", "items": []}
    assert "already running" in s.start_teslafi() and "already running" in s.start_teslamate("postgresql://x", [{"vin": "V", "id": 1}])
    assert s.begin("c.csv", 1)[1] and s.unstage() is None and len(s.view()["staged"]) == 2, "no uploads, removals or dismissals while it runs"
    s.dismiss()
    assert s.state["job"] is not None


def test_starting_with_nothing_staged_says_so(tmp_path):
    assert "Upload" in Store(tmp_path, threaded=False).start_teslafi()


def test_a_running_job_is_interrupted_by_a_restart_and_resumes_the_unfinished_files(tmp_path, export):
    s = staged_store(tmp_path, export, loader=fake_loader())
    s.start_teslafi()
    # what the disk held when the process died: a done, a half-done, a waiting file
    job = s.state["job"]
    job.update(state="running", finished=None, result={"drives": 10, "charges": 4, "days": 30, "positions": 100})
    job["items"][1].update(state="running")
    (s.job_dir / "b.csv").write_bytes(export[0].read_bytes())
    s.save(force=True)

    again = Store(tmp_path, threaded=False)
    again.recover()
    view = again.view()
    assert view["job"]["state"] == "interrupted" and not view["busy"] and "restarted" in view["job"]["step"]
    assert [i["state"] for i in view["job"]["items"]] == ["done", "pending"]
    calls = []
    again.loader = fake_loader(calls)
    assert again.resume() is None
    assert calls == ["b.csv"], "the finished file is not read again"
    assert again.view()["job"]["state"] == "done" and again.view()["job"]["result"]["drives"] == 20


def test_resume_skips_a_file_whose_upload_is_gone(tmp_path, export):
    s = staged_store(tmp_path, export, names=("a.csv",), loader=fake_loader())
    s.start_teslafi()
    s.state["job"].update(state="interrupted")
    s.state["job"]["items"][0].update(state="pending")
    s.resume()
    job = s.view()["job"]
    assert job["items"][0]["state"] == "skipped" and "upload it again" in job["items"][0]["detail"] and job["state"] == "failed"


def test_the_state_survives_a_reload_of_the_page_and_a_new_process(tmp_path, export):
    s = staged_store(tmp_path, export, loader=fake_loader())
    s.start_teslafi()
    again = Store(tmp_path)
    again.recover()
    assert again.view()["job"]["state"] == "done" and again.view()["job"]["result"]["days"] == 60


def test_an_unreadable_state_file_starts_empty(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    s = Store(tmp_path)
    s.recover()
    assert s.view()["job"] is None and s.view()["staged"] == []


def test_a_half_uploaded_file_from_a_dead_process_is_cleared(tmp_path):
    s = Store(tmp_path)
    s.staged_dir.mkdir(parents=True)
    (s.staged_dir / "x.csv.part").write_bytes(b"half")
    s.recover()
    assert not list(s.staged_dir.iterdir())


def test_dismissing_clears_the_job_and_its_files(tmp_path, export):
    s = staged_store(tmp_path, export, loader=fake_loader(bad=("a.csv", "b.csv")))
    s.start_teslafi()
    s.dismiss()
    assert s.view()["job"] is None and not list(s.job_dir.iterdir())


def test_a_job_really_runs_off_the_calling_thread(tmp_path, export):
    import threading
    import time

    gate, who = threading.Event(), []

    def slow(path, progress):
        who.append(threading.current_thread().name)
        gate.wait(5)
        return {"drives": 1}

    s = Store(tmp_path, loader=slow)
    upload(s, "a.csv", export[0].read_bytes())
    assert s.start_teslafi() is None and s.view()["busy"] and s.view()["job"]["state"] == "running"
    gate.set()
    s.thread.join(5)
    assert who == ["history-import"] and s.view()["job"]["state"] == "done"
    time.sleep(0)


# ---- TeslaMate
CARS = [{"id": 1, "vin": "VIN-ALPHA", "name": "Alpha", "model": "3", "trim_badging": None}, {"id": 2, "vin": "VIN-BRAVO", "name": "Bravo", "model": "Y", "trim_badging": None}]


def test_a_teslamate_job_goes_car_by_car_and_keeps_its_address_only_in_memory(tmp_path):
    seen = []

    def mate(url, car, progress):
        seen.append((url, car["vin"]))
        if car["vin"] == "VIN-BRAVO":
            raise Skip("No history for this car.")
        return {"drives": 7, "charges": 2, "days": 90, "positions": 5, "detail": "Alpha"}

    s = Store(tmp_path, threaded=False, mate=mate)
    assert s.start_teslamate("postgresql://u:SECRET@host/tm", CARS) is None
    job = s.view()["job"]
    assert seen == [("postgresql://u:SECRET@host/tm", "VIN-ALPHA"), ("postgresql://u:SECRET@host/tm", "VIN-BRAVO")]
    assert job["state"] == "done" and job["kind"] == "teslamate" and job["items"][1]["state"] == "skipped"
    assert "SECRET" not in (tmp_path / "state.json").read_text() and s.secret is None


def test_an_interrupted_teslamate_job_has_no_resume_only_a_fresh_start(tmp_path):
    s = Store(tmp_path, threaded=False, mate=lambda *a: {})
    s.start_teslamate("postgresql://x", CARS)
    s.state["job"]["state"] = "interrupted"
    assert s.resume() == "Nothing to resume."


def test_teslamate_cars_come_from_the_database_or_a_reason_without_the_password():
    import importroutes

    def refuse(url):
        raise OSError(f"could not connect to {url}")

    cars, why = importroutes.cars_of("postgresql://u:hunter2@host/tm", refuse)
    assert cars == [] and "hunter2" not in why and why.startswith("Could not read that database")
    assert importroutes.cars_of("mysql://x", refuse)[1].startswith("A TeslaMate database address")


# ---- a re-run writes the same import id
def test_loading_a_file_twice_uses_the_same_import_id(tmp_path, export, monkeypatch):
    """The id is the file's hash: the same bytes under any name replace what they loaded."""
    import shutil

    import teslafi_load

    ids = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_):  # the drive count either side of a write
            return self

        def fetchone(self):
            return (0,)

    monkeypatch.setattr(
        teslafi_load, "write_history", lambda conn, vin, name, make, model, cap, out, import_id, *a: ids.append(import_id) or {"trips": len(out["trips"]), "charging_sessions": 1, "cc_positions": 2}
    )
    monkeypatch.setattr(teslafi_load, "fill_after_import", lambda conn, vin, tz: (1, 1))
    monkeypatch.setattr(teslafi_load, "live_since", lambda conn, vin: None)
    load = importjob.load_teslafi_file(Conn)
    first = tmp_path / "first.csv"
    shutil.copy(export[0], first)
    again = tmp_path / "again-renamed.csv"
    shutil.copy(export[0], again)
    got = [load(p, lambda *a: None) for p in (first, again, first)]
    assert len(set(ids)) == 1 and len(ids) == 3
    assert got[0] == got[1] and got[0]["drives"] > 0 and got[0]["days"] >= 3


def test_an_export_stops_where_a_live_source_began(monkeypatch, export, tmp_path):
    """A car the TeslaFi feed or the connector already logs keeps what that source wrote:
    the export is cut where live logging began, and one wholly after it adds nothing."""
    import teslafi_load

    written = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_):  # the drive count either side of a write
            return self

        def fetchone(self):
            return (0,)

    monkeypatch.setattr(teslafi_load, "write_history", lambda conn, vin, name, make, model, cap, out, *a: written.append(out) or {"trips": len(out["trips"])})
    monkeypatch.setattr(teslafi_load, "fill_after_import", lambda conn, vin, tz: (0, 0))
    out = teslafi_load.parse(export[0])
    mid = out["first"] + (out["last"] - out["first"]) / 2
    monkeypatch.setattr(teslafi_load, "live_since", lambda conn, vin: mid)
    got = importjob.load_teslafi_file(Conn)(export[0], lambda *a: None)
    assert written and all(t["start_date"] < mid for t in written[-1]["trips"]) and written[-1]["positions"] and max(p[0] for p in written[-1]["positions"]) < mid
    assert got["drives"] == len(written[-1]["trips"]) < len(out["trips"])
    monkeypatch.setattr(teslafi_load, "live_since", lambda conn, vin: out["first"])
    with pytest.raises(importjob.Skip, match="logged live from"):
        importjob.load_teslafi_file(Conn)(export[0], lambda *a: None)


def test_live_since_is_the_earliest_live_source():
    import history

    t1, t2 = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 5, 1, tzinfo=timezone.utc)

    class Conn:
        def __init__(self, connector, rolling):
            self.answers = {"states": (connector,), "cc_imports": (rolling,)}

        def execute(self, sql, args):
            self.sql = sql
            return self

        def fetchone(self):
            return self.answers["states" if "FROM states" in self.sql else "cc_imports"]

    assert history.live_since(Conn(t2, t1), "V1") == t1, "the feed began first"
    assert history.live_since(Conn(t1, None), "V1") == t1, "the connector alone"
    assert history.live_since(Conn(None, None), "V1") is None, "history only: nothing to stop at"


def test_teslamate_uses_one_import_id_per_car(monkeypatch):
    import teslamate_load

    ids = []
    monkeypatch.setattr(teslamate_load, "write_history", lambda conn, vin, name, make, model, cap, out, import_id, *a: ids.append(import_id) or {})
    monkeypatch.setattr(teslamate_load, "fill_after_import", lambda *a: (0, 0))
    monkeypatch.setattr(teslamate_load, "live_since", lambda conn, vin: None)

    class Conn:
        def execute(self, *a):
            return self

        def commit(self):
            pass

    for _ in range(2):
        teslamate_load.write_car(Conn(), {"trips": [], "charging_sessions": [], "positions": [], "software": []}, "VIN-ALPHA", "Alpha", "Tesla", None, None, "x")
    assert ids == ["tm-VIN-ALPHA", "tm-VIN-ALPHA"]


# ---- the pages
def view(**over):
    base = {"staged": [], "job": None, "notice": None, "max_mb": 1024, "busy": False, "saved_mate": False}
    base.update(over)
    return base


def job(state="running", **over):
    base = {
        "id": "abc",
        "kind": "teslafi",
        "state": state,
        "step": "Reading Alpha.csv.gz: through Mar 2023",
        "pct": 40,
        "error": None,
        "places": None,
        "result": {"drives": 6574, "charges": 2398, "days": 2600, "positions": 9},
        "items": [{"name": "Alpha.csv.gz", "size": 5, "state": "running", "detail": "", "pct": 40}, {"name": "Bravo.csv.gz", "size": 5, "state": "pending", "detail": "", "pct": 0}],
    }
    base.update(over)
    return base


def render(imp, **ctx):
    from test_settings_ui import _env

    return _env().get_template("_import.html").render(imp=imp, root="", csrf="t", **ctx)


def test_the_idle_card_offers_both_ways_in():
    html = render(view())
    assert 'id="imp-teslafi"' in html and 'type="file"' in html and 'multiple' in html and "up to 1024 MB" not in html and ".csv or .csv.gz" in html
    assert 'name="url"' in html and 'type="password"' in html and 'data-busy="0"' in html
    assert "Use the saved database" not in html and "/settings/import/start" not in html


def test_staged_files_are_listed_with_a_remove_and_one_start():
    html = render(view(staged=[{"name": "Alpha.csv.gz", "size": 52_428_800}, {"name": "Bravo.csv", "size": 1_048_576}]))
    assert "Alpha.csv.gz" in html and "50.0 MB" in html and html.count("/settings/import/remove") == 2 and "Import 2 files" in html
    assert "Import 1 file<" in render(view(staged=[{"name": "A.csv", "size": 1}]))


def test_a_saved_teslamate_database_can_be_reused():
    assert 'name="saved" value="1"' in render(view(saved_mate=True))


def test_a_running_job_shows_its_file_its_progress_and_hides_the_intake():
    html = render(view(job=job(), busy=True))
    assert 'data-busy="1"' in html and "Reading Alpha.csv.gz: through Mar 2023" in html and '<progress class="imp" max="100" value="40" data-pct' in html
    assert "waiting" in html and "imp-teslafi" not in html and "imp-teslamate" not in html and "Resume" not in html


def test_a_finished_job_shows_what_was_added_and_what_was_skipped():
    items = [
        {"name": "Alpha.csv.gz", "size": 5, "state": "done", "detail": "Alpha, Mar 2019 to Oct 2026", "pct": 100},
        {"name": "Notes.csv", "size": 5, "state": "skipped", "detail": "No VIN in the file.", "pct": 0},
    ]
    html = render(view(job=job("done", items=items, places=12)))
    assert "6,574</b> drives" in html and "2,398</b> charges" in html and "2,600</b> days" in html and "12</b> new places" in html
    assert "loaded" in html and "skipped" in html and "No VIN in the file." in html and "Alpha, Mar 2019 to Oct 2026" in html
    assert 'value="done"' not in html and ">Close<" in html and "Resume" not in html and "imp-teslafi" in html, "the next import can start under the result"


def test_an_interrupted_teslafi_job_offers_resume_and_start_over_and_a_teslamate_one_only_dismiss():
    html = render(view(job=job("interrupted", step="The site restarted while this was running.")))
    assert "Interrupted" in html and "The site restarted" in html and ">Resume<" in html and ">Start over<" in html and 'name="restart" value="1"' in html and ">Dismiss<" in html
    assert "imp-teslafi" not in html
    mate = render(view(job=job("interrupted", kind="teslamate")))
    assert ">Resume<" not in mate and ">Dismiss<" in mate


def test_a_failed_job_says_why_and_offers_another_try():
    html = render(view(job=job("failed", error="Nothing was imported.")))
    assert "Nothing was imported." in html and ">Try again<" in html and ">Start over<" not in html and ">Resume<" not in html and "imp-teslafi" in html


def test_an_overlap_is_told_in_words_not_by_class_name():
    class Overlap(Exception):
        sqlstate = "23505"

    assert "overlaps history already held" in importjob.explain(Overlap("duplicate key value violates unique constraint"))
    assert "UniqueViolation" not in importjob.explain(Overlap())
    assert "damaged" in importjob.explain(EOFError("Compressed file ended before the end-of-stream marker was reached"))
    assert "did not answer" in importjob.explain(type("OperationalError", (Exception,), {})())


def test_a_job_that_loaded_nothing_carries_the_one_reason(tmp_path, export):
    def overlap(path, progress):
        raise type("UniqueViolation", (Exception,), {"sqlstate": "23505"})("dup")

    s = staged_store(tmp_path, export, names=("a.csv",), loader=overlap)
    s.start_teslafi()
    err = s.view()["job"]["error"]
    assert err.startswith("Nothing was imported: Some of it overlaps history already held") and "UniqueViolation" not in err


def test_a_notice_is_shown_as_an_alert_line():
    assert 'class="notice err" role="status">Upload a TeslaFi export first.' in render(view(notice={"kind": "err", "text": "Upload a TeslaFi export first."}))


def test_the_card_has_no_explanatory_paragraph():
    from test_settings_ui import _Text

    t = _Text()
    t.feed(render(view(staged=[{"name": "A.csv", "size": 1}], notice={"kind": "ok", "text": "x"})))
    assert not [p for p in t.ps if len(" ".join(p.split())) > 100]


def test_the_cars_section_carries_the_card(client, bare, tmp_path, monkeypatch):  # noqa: F811
    r = client.get("/settings/cars")
    assert r.status_code == 200 and 'id="import"' in r.text and "Bring history in" in r.text and 'id="imp-teslafi"' in r.text


# ---- the routes
@pytest.fixture
def store(bare, tmp_path, monkeypatch):  # noqa: F811
    s = bare.IMPORTS
    monkeypatch.setattr(s, "root", tmp_path)
    monkeypatch.setattr(s, "staged_dir", tmp_path / "staged")
    monkeypatch.setattr(s, "job_dir", tmp_path / "job")
    monkeypatch.setattr(s, "state", {"staged": [], "job": None, "notice": None})
    monkeypatch.setattr(s, "threaded", False)
    monkeypatch.setattr(s, "loader", fake_loader())
    monkeypatch.setattr(s, "after", None)
    return s


def token(bare):  # noqa: F811
    import auth

    return auth.csrf_token(bare.AUTH["secret"], None)


def test_the_upload_route_streams_a_file_in_and_start_runs_it(client, bare, store, export):  # noqa: F811
    t = token(bare)
    body = export[1].read_bytes()
    r = client.post("/settings/import/upload?name=Alpha.csv.gz", content=body, headers={"x-csrf-token": t, "content-type": "application/octet-stream"})
    assert r.status_code == 200 and r.json() == {"ok": True, "name": "Alpha.csv.gz"}
    assert (store.staged_dir / "Alpha.csv.gz").read_bytes() == body
    assert client.get("/settings/import/status").json()["staged"][0]["name"] == "Alpha.csv.gz"
    r = client.post("/settings/import/start", data={"csrf_token": t})
    assert r.status_code == 303 and r.headers["location"] == "/settings/cars#import"
    assert client.get("/settings/import/status").json()["job"]["state"] == "done"
    assert "6574" not in client.get("/settings/cars").text and "drives" in client.get("/settings/cars").text


def test_the_upload_route_refuses_a_wrong_file_a_wrong_name_and_a_missing_token(client, bare, store):  # noqa: F811
    t = token(bare)
    h = {"x-csrf-token": t, "content-type": "application/octet-stream"}
    r = client.post("/settings/import/upload?name=x.csv", content=b"a,b\n1,2\n", headers=h)
    assert r.status_code == 400 and "Not a TeslaFi export" in r.json()["error"]
    assert client.post("/settings/import/upload?name=x.exe", content=b"x", headers=h).status_code == 400
    bare.AUTH["enabled"] = True
    try:
        assert client.post("/settings/import/upload?name=x.csv", content=b"x", headers={"content-type": "application/octet-stream"}).status_code in (303, 401, 403)
    finally:
        bare.AUTH["enabled"] = False
    assert not list(store.staged_dir.iterdir())


def test_the_upload_route_stops_at_the_size_cap(client, bare, store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(importjob, "MAX_FILE", 2048)
    h = {"x-csrf-token": token(bare), "content-type": "application/octet-stream"}
    r = client.post("/settings/import/upload?name=x.csv", content=b"Date,battery_level\n" + b"x" * 5000, headers=h)
    assert r.status_code == 413 and "limit" in r.json()["error"] and not list(store.staged_dir.glob("*"))


def test_starting_with_nothing_staged_redirects_with_the_reason(client, bare, store):  # noqa: F811
    r = client.post("/settings/import/start", data={"csrf_token": token(bare)})
    assert r.status_code == 303 and store.view()["notice"]["text"].startswith("Upload")
    assert "Upload a TeslaFi export first." in client.get("/settings/cars").text


def test_a_teslamate_address_that_does_not_answer_starts_nothing(client, bare, store, monkeypatch):  # noqa: F811
    def refuse(url, **_):
        raise OSError("no route")

    monkeypatch.setattr(bare, "_teslamate_connect", refuse)
    r = client.post("/settings/import/teslamate", data={"csrf_token": token(bare), "url": "postgresql://u:pw@nowhere/tm"})
    assert r.status_code == 303
    notice = store.view()["notice"]
    assert notice["kind"] == "err" and "pw@" not in notice["text"] and store.view()["job"] is None
    client.post("/settings/import/teslamate", data={"csrf_token": token(bare), "url": ""})
    assert "address is needed" in store.view()["notice"]["text"]


def test_the_status_route_is_never_cached(client, store):  # noqa: F811
    r = client.get("/settings/import/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and r.json()["job"] is None


def test_a_restart_marks_the_job_interrupted_through_the_apps_own_hook(store):
    store.state["job"] = job("running")
    store.recover()
    assert store.view()["job"]["state"] == "interrupted"


def test_a_finished_import_says_done_once_and_aligns_its_button():
    html = render(view(job=job("done", items=[], places=4)))
    assert html.count(">Close<") == 1 and ">Done<" not in html and "Finished" in html, "the state line says Finished and the button Close"
    import re

    assert re.search(r'class="set-c">\s*<form method="post" action="/settings/import/dismiss"', html), "the button sits in a row, on the right, not loose in the card"


def test_a_finished_import_offers_naming_the_places_and_leaves_the_maps_offer_to_the_pages_that_show_it():
    off = {"maps_offer": None, "public_tiles": False, "public_geocoder": False}
    html = render(view(job=job("done", items=[], places=4)), settings=off)
    assert "Name your places" in html and "4 places found from where the cars stop" in html and 'href="/places"' in html
    assert 'id="maps-offer"' not in html, "Now, Places and the garage ask once; the finished card does not ask again"
    assert "1 place found" in render(view(job=job("done", items=[], places=1)), settings=off)
    assert "Name your places" not in render(view(job=job("done", items=[], places=None)), settings=off)


def test_a_result_counts_in_the_right_number():
    one = render(view(job=job("done", items=[], places=1, result={"drives": 1, "charges": 1, "days": 1, "positions": 0})))
    assert (
        "<b>1</b> drive" in one
        and "<b>1</b> charge " in one
        and "<b>1</b> day" in one
        and "1 charges" not in one
        and "1 days" not in one
        and "1 drives" not in one
        and "new place" in one
        and "new places" not in one
    )
    none = render(view(job=job("done", items=[], result={"drives": 0, "charges": 0, "days": 0, "positions": 0})))
    assert "<b>0</b> drives" in none and "<b>0</b> charges" in none


def test_a_file_size_is_in_kb_under_a_megabyte():
    from filters import filesize

    assert [filesize(n) for n in (0, 1, 51_200, 1_048_000, 1_048_576, 52_428_800)] == ["1 KB", "1 KB", "50 KB", "1,023 KB", "1.0 MB", "50.0 MB"]
    html = render(view(staged=[{"name": "Alpha.csv", "size": 51_200}]))
    assert "50 KB" in html and "0.0 MB" not in html


def test_a_refused_teslamate_address_comes_back_in_the_open_form_once(client, bare, store, monkeypatch):  # noqa: F811
    def refuse(url, **_):
        raise OSError("password authentication failed")

    monkeypatch.setattr(bare, "_teslamate_connect", refuse)
    typed = "postgresql://u:pw@nowhere/tm"
    client.post("/settings/import/teslamate", data={"csrf_token": token(bare), "url": typed})
    page = client.get("/settings/cars").text
    assert f'value="{typed}"' in page and 'id="imp-teslamate" open' in page
    assert 'id="imp-teslamate" open' not in client.get("/settings/cars").text, "handed back once, not kept"
    assert typed not in json.dumps(store.view()), "never part of the saved or polled state"
    client.post("/settings/import/teslamate", data={"csrf_token": token(bare), "url": ""})
    assert store.take_typed() is None

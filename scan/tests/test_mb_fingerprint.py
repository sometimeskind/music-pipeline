"""music-mb-fingerprint --recheck (homelab#2543): a guessed mb_trackid checked against AcoustID.

The cases are the library's: a stereo track beets matched to the 5.1 mix (6994,
and 6930, a merge keeper, replaced like any item), a live album's track matched to the studio alternate
version (5672), and a studio track matched to a live recording (5970).  The
replacement tests run against Postgres when ``MUSIC_TEST_DATABASE_URL`` is set
(fetch/tests/pg_fixture.py).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import psycopg
import pytest

from music_scan import mb_fingerprint as mbf

# fetch/tests in the repo (CI), /app/fetch_tests in the dev image (just test).
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(next((p for p in (_here.parent.parent / "fetch" / "tests", _here.parent / "fetch_tests")
                             if (p / "pg_fixture.py").exists()), _here)))
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

STEREO, SURROUND = "stereo-rec", "surround-rec"
LIVE, ALTERNATE = "live-rec", "alternate-rec"
STUDIO, LIVE_1992, REMASTER = "studio-rec", "live-1992-rec", "remaster-rec"


class FakeItem:
    """Enough of a beets Item: ``get``/``[]`` for every field, ``id``, ``path``, store and write."""

    def __init__(self, id_, title, **data):
        self.id, self.path = id_, f"/lib/{title}-{id_}.m4a".encode()
        self.data = {"title": title, **data}
        self.stored = self.written = False

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def store(self):
        self.stored = True

    def try_write(self):
        self.written = True
        return True


class FakeLib:
    def __init__(self, items):
        self.by_id = {i.id: i for i in items}

    def get_item(self, item_id):
        return self.by_id.get(item_id)


def _recheck(items, lookup, lib=None):
    """recheck over *items*, each re-read from *lib* (by default a library of *items*)."""
    return mbf.recheck(lib or FakeLib(items), [i.id for i in items], lookup)


def _library():
    return [
        FakeItem(6994, "Oh! Sweet Nuthin’ (5.1 mix)", mb_trackid=SURROUND, mb_releasetrackid=SURROUND),
        FakeItem(6930, "Oh! Sweet Nuthin’ (5.1 mix)", mb_trackid=SURROUND, merged_files="633-10 - x.m4a"),
        FakeItem(5672, "The Big Country (alternate version)", mb_trackid=ALTERNATE),
        FakeItem(5970, "Purple Rain", mb_trackid=LIVE_1992),
    ]


ACOUSTID = {
    6994: [(0.97, STEREO)],
    6930: [(0.96, STEREO), (0.40, SURROUND)],  # a weak sibling doesn't count
    5672: [(0.95, LIVE)],
    5970: [(0.98, STUDIO), (0.91, REMASTER)],
}


def _lookup(table=None):
    table = ACOUSTID if table is None else table
    calls = []

    def lookup(path: str):
        item_id = int(path.rsplit("-", 1)[1].split(".")[0])
        calls.append(item_id)
        return table[item_id]

    lookup.calls = calls
    return lookup


def test_flags_each_guess_not_among_its_recordings(caplog):
    caplog.set_level("INFO")
    counts, flagged = _recheck(_library(), _lookup())
    assert {f.item_id: f.replacement for f in flagged} == {6994: STEREO, 6930: STEREO, 5672: LIVE, 5970: None}
    assert counts["checked"] == 4 and counts["flagged"] == 4 and counts["agree"] == 0
    # 5970 has two recordings; 6930, a merge keeper, is replaced like the others (homelab#2543, 2026-10-31).
    assert counts["replaceable"] == 3 and counts["keepers"] == 1 and counts["ambiguous"] == 1
    assert ("[RECHECK] 6994 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec is not among its "
            "AcoustID recordings (stereo-rec 0.97): → stereo-rec") in caplog.text
    assert "6930 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec is not among" in caplog.text
    assert "(stereo-rec 0.96): → stereo-rec (a merge keeper)" in caplog.text
    assert "[RECHECK] 5672 — The Big Country (alternate version): mb_trackid alternate-rec" in caplog.text
    assert ("[RECHECK] 5970 — Purple Rain: mb_trackid live-1992-rec is not among its AcoustID recordings "
            "(studio-rec 0.98): 2 recordings (studio-rec 0.98, remaster-rec 0.91), none chosen; left alone"
            ) in caplog.text


def test_a_guess_among_its_recordings_is_not_reported(caplog):
    caplog.set_level("INFO")
    # AcoustID links the fingerprint to the stereo and the 5.1 recording: the guess stands.
    counts, flagged = _recheck(_library()[:1], _lookup({6994: [(0.97, STEREO), (0.90, SURROUND)]}))
    assert flagged == [] and counts["agree"] == 1 and caplog.text == ""


@pytest.mark.parametrize("found,best", [([], "no recording"), ([(0.6, STEREO)], "best stereo-rec 0.60")])
def test_nothing_confident_is_nomatch_and_left_alone(caplog, found, best):
    caplog.set_level("INFO")
    counts, flagged = _recheck(_library()[:1], _lookup({6994: found}))
    assert flagged == [] and counts["nomatch"] == 1
    assert (f"[NOMATCH] 6994 — Oh! Sweet Nuthin’ (5.1 mix): AcoustID has nothing at 0.85 or more ({best}); "
            "mb_trackid surround-rec left alone") in caplog.text


def test_a_request_fallback_and_an_item_without_an_id_are_not_looked_up():
    items = [FakeItem(1, "Fallback", mb_trackid=STEREO, fallback_recording=STEREO), FakeItem(2, "No ID")]
    lookup = _lookup({})
    counts, flagged = _recheck(items, lookup)
    assert lookup.calls == [] and flagged == [] and counts["skipped"] == 1 and counts["checked"] == 0


def test_a_failed_lookup_is_retried_once_then_an_error(monkeypatch):
    monkeypatch.setattr(mbf, "RETRY_SECONDS", 0)
    tries = []

    def flaky(path):
        tries.append(path)
        if len(tries) == 1:
            raise mbf.LookupFailed("503")
        return [(0.97, STEREO)]

    counts, flagged = _recheck(_library()[:1], flaky)
    assert len(tries) == 2 and counts["errors"] == 0 and [f.replacement for f in flagged] == [STEREO]

    def down(path):
        raise mbf.LookupFailed("503")

    counts, _ = _recheck(_library()[:1], down)
    assert counts["errors"] == 1 and counts["checked"] == 0


def test_lookups_failing_in_a_row_stop_the_run(monkeypatch):
    monkeypatch.setattr(mbf, "RETRY_SECONDS", 0)
    monkeypatch.setattr(mbf, "MAX_FAILURES_IN_A_ROW", 3)

    def down(path):
        raise mbf.LookupFailed("invalid API key")

    items = [FakeItem(n, f"T{n}", mb_trackid=STEREO) for n in range(1, 6)]
    with pytest.raises(SystemExit, match="3 AcoustID lookups failed in a row"):
        _recheck(items, down)


@pytest.mark.parametrize("exc", [ValueError("no fingerprint: decode error"), PermissionError("denied")])
def test_a_file_without_a_fingerprint_is_an_error_not_a_failure_in_a_row(monkeypatch, tmp_path, exc):
    monkeypatch.setattr(mbf, "MAX_FAILURES_IN_A_ROW", 2)

    def unreadable(path):
        raise exc

    items = [FakeItem(n, f"T{n}", mb_trackid=STEREO) for n in range(1, 6)]
    for i in items:  # the files are there
        i.path = bytes(tmp_path / f"T-{i.id}.m4a")
        (tmp_path / f"T-{i.id}.m4a").touch()
    counts, _ = _recheck(items, unreadable)
    assert counts["errors"] == 5 and counts["gone"] == 0


# --- a whole-library run takes hours, and the pipeline's ticks go on meanwhile ---


def test_an_item_is_read_just_before_its_lookup(caplog):
    """A tick's canon [RETAG] moved 5672's album after the run started (homelab#2543, 2026-10-31):
    its lookup uses the path the item has then, not the one it had at the start."""
    caplog.set_level("INFO")
    items = _library()
    lib = FakeLib(items)
    paths = []

    def lookup(path):
        paths.append(path)
        if path.endswith("-6994.m4a"):  # meanwhile, a tick moves 5672 and retires 5970
            lib.by_id[5672].path = b"/lib/The Big Country (remastered)-5672.m4a"
            del lib.by_id[5970]
        return _lookup()(path)

    counts, flagged = _recheck(items, lookup, lib)
    assert paths[-1] == "/lib/The Big Country (remastered)-5672.m4a"
    assert [f.item_id for f in flagged] == [6994, 6930, 5672]
    assert counts["checked"] == 3 and counts["gone"] == 1
    assert "[GONE] 5970: no longer in the library; not checked" in caplog.text


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "No such file or directory"),
                                 ValueError("no fingerprint: fpcalc exited with status 2")])
def test_a_file_that_goes_away_mid_lookup_is_gone_not_a_crash(caplog, exc):
    """The file moved between the item's read and its fingerprint (audioread raises
    FileNotFoundError; fpcalc's failure is a ValueError): counted as gone, and the run goes on."""
    caplog.set_level("INFO")
    items = _library()

    def lookup(path):
        if path.endswith("-6994.m4a"):
            raise exc
        return _lookup()(path)

    counts, flagged = _recheck(items, lookup)
    assert counts["gone"] == 1 and counts["errors"] == 0 and counts["checked"] == 3
    assert [f.item_id for f in flagged] == [6930, 5672, 5970]
    assert ("[GONE] 6994 — Oh! Sweet Nuthin’ (5.1 mix): its file went away during the run; not checked "
            "(re-run with its ID)") in caplog.text


def test_progress_is_logged_every_so_many_items(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "PROGRESS_EVERY", 2)
    _recheck(_library(), _lookup())
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("[PROGRESS]")] == [
        "[PROGRESS] 2/4, 2 flagged so far"]


def test_apply_refuses_without_the_database(monkeypatch):
    monkeypatch.delenv("MUSIC_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="MUSIC_DATABASE_URL is unset"):
        mbf.run_recheck(apply=True, lookup=_lookup())


def test_main_keeps_apply_to_recheck(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", "--apply"])
    monkeypatch.setattr(mbf, "run", lambda: pytest.fail("the fill mode has no --apply"))
    with pytest.raises(SystemExit):
        mbf.main()


# --- replacing, against Postgres ---


def _events(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT kind, source, item_id, message, data FROM events ORDER BY id").fetchall()


@needs_pg
def test_replace_stores_each_replaceable_guess_with_its_event(pg_url, monkeypatch):
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    items = _library()
    _, flagged = _recheck(items, _lookup())
    assert mbf.replace(FakeLib(items), flagged) == 3
    by_id = {i.id: i for i in items}
    assert by_id[6994]["mb_trackid"] == STEREO and by_id[6994].stored and by_id[6994].written
    assert by_id[6994]["mb_releasetrackid"] == SURROUND  # kept as it was
    assert by_id[5672]["mb_trackid"] == LIVE
    assert by_id[6930]["mb_trackid"] == STEREO and by_id[6930].stored  # a merge keeper too
    assert by_id[5970]["mb_trackid"] == LIVE_1992 and not by_id[5970].stored  # two recordings
    rows = _events(pg_url)
    assert [(k, s, i) for k, s, i, _, _ in rows] == [("recording_replaced", "mb-fingerprint", n)
                                                     for n in (6994, 6930, 5672)]
    assert rows[0][3] == ("[RECHECK] 6994 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec → stereo-rec "
                          "(AcoustID 0.97)")
    assert rows[0][4]["old"] == SURROUND and rows[0][4]["new"] == STEREO
    assert [r[4]["keeper"] for r in rows] == [False, True, False]


@needs_pg
def test_replace_skips_an_item_changed_since_its_lookup(pg_url, monkeypatch, caplog):
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    items = _library()
    _, flagged = _recheck(items, _lookup())
    items[0]["mb_trackid"] = "set-by-hand"
    lib = FakeLib(items)
    del lib.by_id[5672]  # it left the library meanwhile
    assert mbf.replace(lib, flagged) == 1  # 6930 only
    assert items[0]["mb_trackid"] == "set-by-hand" and [r[2] for r in _events(pg_url)] == [6930]
    assert "[SKIP] 6994: mb_trackid is set-by-hand now, not surround-rec" in caplog.text
    assert "[SKIP] 5672: gone from the library; left alone" in caplog.text


@needs_pg
def test_a_store_that_fails_leaves_no_event(pg_url, monkeypatch):
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    items = _library()
    _, flagged = _recheck(items, _lookup())

    def locked():
        raise RuntimeError("database is locked")

    items[0].store = locked
    with pytest.raises(RuntimeError, match="database is locked"):
        mbf.replace(FakeLib(items), flagged)
    assert _events(pg_url) == []


# --- a recorded run (homelab#2553): checkpoints, resume, the apply by run ID ---


class Killed(BaseException):
    """The process dying mid-run (a rollout): no handler runs."""


class RunLib(FakeLib):
    """A FakeLib run_recheck and replace_run can open."""

    def items(self):
        return list(self.by_id.values())

    def _close(self):
        pass


def test_a_checkpoint_every_so_many_items_and_after_the_last(monkeypatch):
    monkeypatch.setattr(mbf, "CHECKPOINT_EVERY", 3)
    items = sorted(_library(), key=lambda i: i.id)
    calls = []
    mbf.recheck(FakeLib(items), [i.id for i in items], _lookup(),
                checkpoint=lambda n, last, counts, new: calls.append((n, last, counts["checked"],
                                                                      [f.item_id for f in new])))
    assert calls == [(3, 6930, 3, [5672, 5970, 6930]), (4, 6994, 4, [6994])]


def test_a_continued_run_counts_on_from_its_checkpoint(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "PROGRESS_EVERY", 3)
    items = _library()[:2]
    counts, flagged = mbf.recheck(FakeLib(items), [i.id for i in items], _lookup(), start=2, total=4,
                                  counts={"checked": 2, "flagged": 1, "nomatch": 1})
    assert counts["checked"] == 4 and counts["flagged"] == 3 and counts["nomatch"] == 1 and len(flagged) == 2
    assert "[PROGRESS] 3/4, 2 flagged so far" in caplog.text


def test_an_item_whose_file_changed_since_its_check_is_skipped(tmp_path):
    """replace_item swaps the audio and keeps mb_trackid (homelab#2553): the ID alone can't tell."""
    item = FakeItem(6994, "T", mb_trackid=SURROUND)
    item.path = bytes(tmp_path / "T-6994.m4a")
    (tmp_path / "T-6994.m4a").write_bytes(b"old audio")
    _, (f,) = mbf.recheck(FakeLib([item]), [6994], _lookup())
    assert f.file[1] == 9 and mbf.verdict(item, f) == (mbf.REPLACE, "")
    (tmp_path / "T-6994.m4a").write_bytes(b"the new, longer audio")
    assert mbf.verdict(item, f) == (mbf.SKIP, "its file changed since its check; recheck it by ID")
    item["mb_trackid"] = STEREO
    assert mbf.verdict(item, f)[0] == mbf.ALREADY
    assert mbf.verdict(None, f) == (mbf.SKIP, "gone from the library")


def test_main_keeps_a_recorded_run_a_dry_run(monkeypatch):
    for argv in (["--recheck", "--run-id", "r1", "--apply"], ["--replace-run", "r1", "--recheck"],
                 ["--run-id", "r1"]):
        monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", *argv])
        monkeypatch.setattr(mbf, "run", lambda: pytest.fail("not the fill mode"))
        monkeypatch.setattr(mbf, "run_recheck", lambda **_: pytest.fail("refused by argparse"))
        with pytest.raises(SystemExit):
            mbf.main()


def _open(monkeypatch, lib):
    import beets.library

    monkeypatch.setattr(beets.library, "Library", lambda *a, **k: lib)
    monkeypatch.delenv("PREFECT_API_URL", raising=False)


def _rows(url, sql, *params):
    with psycopg.connect(url) as conn:
        return conn.execute(sql, params).fetchall()


@needs_pg
def test_a_recorded_run_resumes_after_its_checkpoint(pg_url, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "CHECKPOINT_EVERY", 2)
    lib = RunLib(_library())
    _open(monkeypatch, lib)
    calls = []

    def dies_at_6930(path):
        item_id = int(path.rsplit("-", 1)[1].split(".")[0])
        if item_id == 6930:
            raise Killed
        calls.append(item_id)
        return ACOUSTID[item_id]

    with pytest.raises(Killed):
        mbf.run_recheck(run_id="r1", lookup=dies_at_6930)
    assert calls == [5672, 5970]  # in ID order
    assert _rows(pg_url, "SELECT status, position, cursor, total FROM long_runs") == [("running", 2, 5970, 4)]
    assert [r[0] for r in _rows(pg_url, "SELECT item_id FROM recheck_findings ORDER BY item_id")] == [5672, 5970]

    resumed = _lookup()
    counts = mbf.run_recheck(run_id="r1", lookup=resumed)
    assert resumed.calls == [6930, 6994]  # only the items after the checkpoint
    assert counts["checked"] == 4 and counts["flagged"] == 4 and counts["replaceable"] == 3
    assert "[RESUME] run r1: continuing after item 5970 (2 of 4 walked, 2 flagged so far)" in caplog.text
    (status, finished), = _rows(pg_url, "SELECT status, finished_at FROM long_runs")
    assert status == "done" and finished is not None
    findings = _rows(pg_url, "SELECT item_id, replacement FROM recheck_findings ORDER BY item_id")
    assert findings == [(5672, LIVE), (5970, None), (6930, STEREO), (6994, STEREO)]
    assert _events(pg_url) == []  # a recorded run is a dry run


@needs_pg
def test_a_recorded_run_refuses_a_finished_run_other_args_and_a_second_process(pg_url, monkeypatch):
    from music_fetch import db
    from music_scan import recheck_runs

    lib = RunLib(_library())
    _open(monkeypatch, lib)
    with psycopg.connect(pg_url, autocommit=True) as other:
        db.migrate(other)
        recheck_runs.hold(other, "r1")
        with pytest.raises(SystemExit, match="run r1 is running in another process"):
            mbf.run_recheck(run_id="r1", lookup=_lookup())
    mbf.run_recheck(run_id="r1", ids=[6994], lookup=_lookup())
    with pytest.raises(SystemExit, match="run r1 finished at"):
        mbf.run_recheck(run_id="r1", ids=[6994], lookup=_lookup())
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE long_runs SET status = 'failed', finished_at = NULL, position = 0, cursor = NULL")
    with pytest.raises(SystemExit, match=r"run r1 was started with \{'ids': \[6994\]\}, not \{'ids': \[5672\]\}"):
        mbf.run_recheck(run_id="r1", ids=[5672], lookup=_lookup())
    resumed = _lookup()
    mbf.run_recheck(run_id="r1", lookup=resumed)  # no IDs: the ones it was started with
    assert resumed.calls == [6994]
    with pytest.raises(SystemExit, match="1 to 64 letters"):
        mbf.run_recheck(run_id="-apply", lookup=_lookup())


@needs_pg
def test_lookups_failing_in_a_row_leave_a_failed_run_to_continue(pg_url, monkeypatch):
    monkeypatch.setattr(mbf, "RETRY_SECONDS", 0)
    monkeypatch.setattr(mbf, "MAX_FAILURES_IN_A_ROW", 2)
    _open(monkeypatch, RunLib(_library()))

    def down(path):
        raise mbf.LookupFailed("503")

    with pytest.raises(SystemExit, match="2 AcoustID lookups failed in a row"):
        mbf.run_recheck(run_id="r1", lookup=down)
    assert _rows(pg_url, "SELECT status FROM long_runs") == [("failed",)]
    assert mbf.run_recheck(run_id="r1", lookup=_lookup())["checked"] == 4


def _recorded_run(pg_url, monkeypatch, lib, ids=None):
    _open(monkeypatch, lib)
    mbf.run_recheck(run_id="r1", ids=ids, lookup=_lookup())


@needs_pg
def test_replace_run_previews_then_applies_a_recorded_run(pg_url, monkeypatch, caplog):
    caplog.set_level("INFO")
    items = _library()
    lib = RunLib(items)
    _recorded_run(pg_url, monkeypatch, lib)
    locks = []
    from music_scan import audit

    @contextlib.contextmanager
    def lock():
        locks.append("pipeline")
        yield

    monkeypatch.setattr(audit, "pipeline_lock", lock)
    preview = mbf.replace_run("r1")
    assert preview["replaceable"] == 3 and preview["replaced"] == 0 and locks == [] and _events(pg_url) == []
    assert "[WOULD-REPLACE] 6994 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec → stereo-rec" in caplog.text
    assert "3 replaceable: 3 to replace (re-run with --apply), 0 already replaced, 0 skipped; 1 with several" \
        in caplog.text

    monkeypatch.setenv("MUSIC_EVENT_JOB", "job-1")
    counts = mbf.replace_run("r1", apply=True)
    assert counts["replaced"] == 3 and locks == ["pipeline"]
    by_id = {i.id: i for i in items}
    assert by_id[6994]["mb_trackid"] == STEREO and by_id[6994].written and by_id[5672]["mb_trackid"] == LIVE
    assert by_id[5970]["mb_trackid"] == LIVE_1992 and not by_id[5970].stored
    rows = _events(pg_url)
    assert [r[2] for r in rows] == [5672, 6930, 6994] and {r[4]["run"] for r in rows} == {"r1"}
    assert _rows(pg_url, "SELECT item_id, outcome, outcome_job FROM recheck_findings "
                         "WHERE replacement IS NOT NULL ORDER BY item_id") == [
        (5672, "replaced", "job-1"), (6930, "replaced", "job-1"), (6994, "replaced", "job-1")]


@needs_pg
def test_an_interrupted_apply_is_finished_by_running_it_again(pg_url, monkeypatch, caplog):
    """A restart mid-apply (homelab#2553): the second apply takes the pipeline lock
    again and re-checks every item, so an item changed in between is skipped, an
    item stored before the kill is written again without a second event, and an
    item whose store died before its transaction committed gets its event."""
    caplog.set_level("INFO")
    items = _library()
    lib = RunLib(items)
    _recorded_run(pg_url, monkeypatch, lib)
    by_id = {i.id: i for i in items}
    from music_scan import audit

    locks = []

    @contextlib.contextmanager
    def lock():
        locks.append("pipeline")
        yield

    monkeypatch.setattr(audit, "pipeline_lock", lock)

    def dies():  # 6930's store: the beets row is written, then the process dies before the commit
        by_id[6930].stored = True
        raise Killed

    by_id[6930].store = dies
    with pytest.raises(Killed):
        mbf.replace_run("r1", apply=True)
    assert [r[2] for r in _events(pg_url)] == [5672]  # 6930's event rolled back with its transaction
    assert by_id[6930]["mb_trackid"] == STEREO  # but beets holds the replacement

    del by_id[6930].store
    by_id[5672].written = False
    by_id[6994]["mb_trackid"] = "set-by-hand"  # changed between the two applies
    counts = mbf.replace_run("r1", apply=True)
    assert locks == ["pipeline", "pipeline"]
    assert counts["already"] == 2 and counts["skipped"] == 1 and counts["replaced"] == 0
    assert by_id[5672].written  # its tags written again
    assert by_id[6994]["mb_trackid"] == "set-by-hand"
    assert "[SKIP] 6994: mb_trackid is set-by-hand now, not surround-rec; left alone" in caplog.text
    assert sorted(r[2] for r in _events(pg_url)) == [5672, 6930]  # one event each, 6930's recorded now
    assert _rows(pg_url, "SELECT item_id, outcome, outcome_reason FROM recheck_findings "
                         "WHERE replacement IS NOT NULL ORDER BY item_id") == [
        (5672, "replaced", None), (6930, "replaced", None),
        (6994, "skipped", "mb_trackid is set-by-hand now, not surround-rec")]


@needs_pg
def test_replace_run_refuses_an_unknown_an_unfinished_and_a_stale_run(pg_url, monkeypatch):
    lib = RunLib(_library())
    _open(monkeypatch, lib)
    with pytest.raises(SystemExit, match="no run r1"):
        mbf.replace_run("r1")
    mbf.run_recheck(run_id="r1", lookup=_lookup())
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE long_runs SET status = 'running'")
    with pytest.raises(SystemExit, match="run r1 is running, not finished"):
        mbf.replace_run("r1", apply=True)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE long_runs SET status = 'done', finished_at = now() - interval '8 days'")
    with pytest.raises(SystemExit, match="more than 7 days ago; start a new run"):
        mbf.replace_run("r1", apply=True)
    assert _events(pg_url) == []


@needs_pg
def test_runs_are_pruned_with_their_findings(pg_url, monkeypatch):
    from music_fetch import db
    from music_scan import recheck_runs

    _recorded_run(pg_url, monkeypatch, RunLib(_library()))
    with contextlib.closing(db.connect()) as conn:
        assert recheck_runs.prune(conn) == 0
        conn.execute("UPDATE long_runs SET finished_at = now() - interval '31 days'")
        assert recheck_runs.prune(conn) == 1
    assert _rows(pg_url, "SELECT count(*) FROM recheck_findings") == [(0,)]

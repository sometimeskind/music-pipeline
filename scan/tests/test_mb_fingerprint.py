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

    def items(self):
        return list(self.by_id.values())


# MusicBrainz's recordings (homelab#2544): what the apply refreshes title and trackdisambig from.
RECORDINGS = {
    STEREO: {"id": STEREO, "title": "Oh! Sweet Nuthin’", "disambiguation": ""},
    LIVE: {"id": LIVE, "title": "The Big Country", "disambiguation": "live, 1979-08-27: Central Park, New York"},
}


def _recordings(table=None, fails=()):
    """A MusicBrainz lookup over *table* (None for a recording it lacks), raising for the IDs in *fails*."""
    table = RECORDINGS if table is None else table
    calls = []

    def lookup(recording_id):
        calls.append(recording_id)
        if recording_id in fails:
            raise OSError("HTTP Error 502: Bad Gateway")
        return table.get(recording_id)

    lookup.calls = calls
    return lookup


@pytest.fixture(autouse=True)
def musicbrainz(monkeypatch):
    """No test reaches MusicBrainz: the refresh's lookups read :data:`RECORDINGS`."""
    lookup = _recordings()
    monkeypatch.setattr(mbf, "musicbrainz_recordings", lambda: lookup)
    return lookup


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


@pytest.mark.parametrize("argv", [[], ["--apply"], ["--run-id", "r1"], ["12"]])
def test_bare_command_writes_nothing(monkeypatch, capsys, argv):
    """No mode flag (homelab#2640): usage and exit 2, no library opened, no lookup."""
    monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", *argv])
    monkeypatch.setattr(mbf, "run_recheck", lambda **_: pytest.fail("not --recheck"))
    monkeypatch.setattr(mbf, "acoustid_recordings", lambda *_: pytest.fail("no AcoustID lookup"))
    assert not hasattr(mbf, "run")
    with pytest.raises(SystemExit) as exc:
        mbf.main()
    assert exc.value.code == 2
    assert "--recheck" in capsys.readouterr().err


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


# --- the guessed recording's fields, refreshed after a replacement (homelab#2544) ---


CONSTRUCTIVE = {"id": "9691e78a", "title": "Constructive Summer", "disambiguation": ""}


def test_a_replaced_items_trackdisambig_is_the_new_recordings():
    """3778 kept the guessed live recording's trackdisambig after #2543's apply."""
    item = FakeItem(3778, "Constructive Summer", via="spotdl",
                    trackdisambig="live, 2024-03-10: Omeara, London, England")
    assert mbf.refreshed(item, CONSTRUCTIVE) == {
        "trackdisambig": ("live, 2024-03-10: Omeara, London, England", "")}
    assert mbf.refreshed(FakeItem(3778, "Constructive Summer", via="spotdl"), CONSTRUCTIVE) == {}


def test_a_spotdl_items_title_is_the_new_recordings():
    """spotdl items' titles came from the guess: 5672's alternate version is the live take."""
    item = FakeItem(5672, "The Big Country (alternate version)", via="spotdl")
    assert mbf.refreshed(item, RECORDINGS[LIVE]) == {
        "title": ("The Big Country (alternate version)", "The Big Country"),
        "trackdisambig": ("", "live, 1979-08-27: Central Park, New York")}


@pytest.mark.parametrize(("title", "recording"), [
    ("In My Time Of Need", {"title": "In My Time of Need", "disambiguation": ""}),  # its spelling, music-pipeline#402
    ("Purple Rain (2015 Paisley Park Remaster)", {"title": "Purple Rain", "disambiguation": ""}),  # no version word
    ("Tangled Up in Blue (Live)", {"title": "Tangled Up in Blue", "disambiguation": "live, 1975-11-21: Boston"}),
    ("Song (5.1 Mix)", {"title": "Song (5.1 mix)", "disambiguation": ""}),  # the new recording's title says it
    ("ロマンス", {"title": "Romance", "disambiguation": ""}),  # nothing to tell two songs apart by
])
def test_a_usenet_items_same_song_title_is_kept(title, recording):
    """Since music-pipeline#398 a Usenet album track's title is its release file's own, on purpose."""
    assert "title" not in mbf.refreshed(FakeItem(1, title, via="usenet"), recording)


@pytest.mark.parametrize(("title", "recording", "after"), [
    # Imported before #398: the guess's version note, which the new recording doesn't have.
    ("Oh! Sweet Nuthin’ (5.1 mix)", RECORDINGS[STEREO], "Oh! Sweet Nuthin’"),
    ("Purple Rain (live)", {"title": "Purple Rain", "disambiguation": ""}, "Purple Rain"),
    # Another song (the Dylan Live 1975 case): the recording's title.
    ("It Ain't Me, Babe (Live At Harvard Square Theatre, Cambridge, MA-November 1975)",
     {"title": "Romance in Durango", "disambiguation": "live, 1975-11-20: Harvard Square Theatre"},
     "Romance in Durango"),
])
def test_a_usenet_items_title_naming_another_version_or_song_is_replaced(title, recording, after):
    assert mbf.refreshed(FakeItem(1, title, via="usenet"), recording)["title"] == (title, after)


def test_a_failed_or_empty_lookup_refreshes_nothing(caplog):
    item = FakeItem(6994, "Oh! Sweet Nuthin’ (5.1 mix)", trackdisambig="5.1 mix")
    assert mbf._refresh(item, STEREO, _recordings(fails={STEREO})) is None
    assert mbf._refresh(item, "merged-away", _recordings()) is None
    assert ("[NOREFRESH] 6994 — Oh! Sweet Nuthin’ (5.1 mix): the MusicBrainz lookup of stereo-rec failed "
            "(HTTP Error 502: Bad Gateway); title and trackdisambig left as they were") in caplog.text
    assert "[NOREFRESH] 6994 — Oh! Sweet Nuthin’ (5.1 mix): MusicBrainz has no recording merged-away" in caplog.text
    assert item.data == {"title": "Oh! Sweet Nuthin’ (5.1 mix)", "trackdisambig": "5.1 mix"}


@pytest.mark.parametrize(("item", "recording"), [
    # An AcoustID collision: a Bollywood recording for a Clap Your Hands Say Yeah track.
    (FakeItem(9127, "The Skin of My Yellow Country Teeth", via="spotdl"),
     {"id": "bolly", "title": "Tu Is Tarah Se Mere Zindagi Main", "disambiguation": ""}),
    # A wrong download, or a wrong match, from the same show.
    (FakeItem(8276, "Out in the Street (live at Brendan Byrne Arena, E. Rutherford, NJ - 8/6/1984)", via="spotdl"),
     {"id": "dark", "title": "Dancing in the Dark", "disambiguation": "live, 1984-08-06: Brendan Byrne Arena"}),
    # A Usenet title from before #398 naming the guess's song wants a person too (a reimport).
    (FakeItem(11982, "It Ain't Me, Babe (Live At Harvard Square Theatre)", via="usenet"),
     {"id": "durango", "title": "Romance in Durango", "disambiguation": "live, 1975-11-20"}),
])
def test_a_recording_naming_another_song_is_suspect_and_refreshes_nothing(item, recording, caplog):
    """homelab#2544: the 2026-10-31 dry run would have retitled these as other songs."""
    before = dict(item.data)
    changes = mbf._refresh(item, recording["id"], _recordings({recording["id"]: recording}))
    assert isinstance(changes, mbf.Suspect) and not changes
    assert item.data == before
    assert f"[SUSPECT] {item.id} — " in caplog.text and repr(recording["title"]) in caplog.text


def test_a_long_title_for_the_same_song_is_no_suspect():
    """6389: MusicBrainz's longer title for the same song refreshes as usual."""
    item = FakeItem(6389, "Midnight Moon Pool", via="spotdl")
    recording = {"id": "moon", "title": "I. Midnight Moon Pool (Womb of the Soul)", "disambiguation": ""}
    assert mbf._refresh(item, "moon", _recordings({"moon": recording})) == {
        "title": ("Midnight Moon Pool", "I. Midnight Moon Pool (Womb of the Soul)")}


@needs_pg
def test_refresh_replaced_lists_suspects_and_leaves_them(pg_url, monkeypatch, caplog):
    caplog.set_level("INFO")
    bolly = {"id": "bolly", "title": "Tu Is Tarah Se Mere Zindagi Main", "disambiguation": ""}
    monkeypatch.setattr(mbf, "musicbrainz_recordings",
                        lambda: _recordings({**RECORDINGS, CONSTRUCTIVE["id"]: CONSTRUCTIVE, "bolly": bolly}))
    items = [FakeItem(9127, "The Skin of My Yellow Country Teeth", via="spotdl", mb_trackid="bolly"),
             FakeItem(3778, "Constructive Summer", via="spotdl", mb_trackid=CONSTRUCTIVE["id"],
                      trackdisambig="live, 2024-03-10: Omeara, London, England")]
    _open(monkeypatch, RunLib(items))
    _replaced(pg_url, (9127, "bolly"), (3778, CONSTRUCTIVE["id"]))

    counts = mbf.refresh_replaced(apply=True)
    assert (counts["refreshed"], counts["suspect"]) == (1, 1)
    assert items[0]["title"] == "The Skin of My Yellow Country Teeth" and not items[0].stored
    assert ("1 suspect (the new recording names another song, left alone: 9127; check each by hand)"
            in caplog.text)


@needs_pg
def test_the_apply_refreshes_each_replaced_items_fields_with_its_event(pg_url, monkeypatch, caplog, musicbrainz):
    caplog.set_level("INFO")
    items = _library()
    items[2]["trackdisambig"] = "alternate version"
    lib = RunLib(items)
    _recorded_run(pg_url, monkeypatch, lib)
    mbf.replace_run("r1")
    assert musicbrainz.calls == []  # the dry run reads only the findings

    counts = mbf.replace_run("r1", apply=True)
    assert counts["replaced"] == 3 and counts["refreshed"] == 3 and counts["norefresh"] == 0
    by_id = {i.id: i for i in items}
    assert (by_id[5672]["title"], by_id[5672]["trackdisambig"], by_id[5672]["mb_trackid"]) == (
        "The Big Country", "live, 1979-08-27: Central Park, New York", LIVE)
    assert by_id[6994]["title"] == "Oh! Sweet Nuthin’" and by_id[6994].written
    assert by_id[5970]["title"] == "Purple Rain"  # not replaced, not refreshed
    data = {r[2]: r[4] for r in _events(pg_url)}
    assert data[5672]["refreshed"] == {"title": ["The Big Country (alternate version)", "The Big Country"],
                                       "trackdisambig": ["alternate version",
                                                         "live, 1979-08-27: Central Park, New York"]}
    assert data[5672]["title"] == "The Big Country (alternate version)"  # the event names the item as it was
    assert ("[REFRESH] 5672: title 'The Big Country (alternate version)' → 'The Big Country'; "
            "trackdisambig 'alternate version' → 'live, 1979-08-27: Central Park, New York'") in caplog.text


@needs_pg
def test_a_failed_lookup_leaves_the_guess(pg_url, monkeypatch, caplog):
    """homelab#2583: the lookup is the check that the new recording is the same song, so
    an item it couldn't check keeps its guess until a run again."""
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "musicbrainz_recordings", lambda: _recordings(fails={STEREO}))
    items = _library()
    _, flagged = _recheck(items, _lookup())
    assert mbf.replace(FakeLib(items), flagged) == 1
    by_id = {i.id: i for i in items}
    assert by_id[6994]["mb_trackid"] == SURROUND and by_id[6994]["title"] == "Oh! Sweet Nuthin’ (5.1 mix)"
    assert by_id[5672]["mb_trackid"] == LIVE and by_id[5672]["title"] == "The Big Country"
    assert [r[2] for r in _events(pg_url)] == [5672]
    assert "[NOREFRESH] 6994" in caplog.text
    assert ("[SKIP] 6994 — Oh! Sweet Nuthin’ (5.1 mix): MusicBrainz couldn't be asked whether its new recording "
            f"is the same song; run it again; mb_trackid {SURROUND} left alone") in caplog.text


ANOTHER_SONG = {**RECORDINGS, STEREO: {"id": STEREO, "title": "Tu Is Tarah Se Mere Zindagi Main", "disambiguation": ""}}


@needs_pg
def test_a_recording_naming_another_song_is_not_replaced(pg_url, monkeypatch, caplog):
    """homelab#2583: the 2026-10-31 apply gave 9127 *The Skin of My Yellow Country Teeth*
    a recording titled "Tu Is Tarah Se Mere Zindagi Main" (an AcoustID collision)."""
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "musicbrainz_recordings", lambda: _recordings(ANOTHER_SONG))
    items = _library()
    _, flagged = _recheck(items, _lookup())
    assert mbf.replace(FakeLib(items), flagged) == 1
    by_id = {i.id: i for i in items}
    assert by_id[6994]["mb_trackid"] == SURROUND and not by_id[6994].written
    assert [r[2] for r in _events(pg_url)] == [5672]
    assert "[SUSPECT] 6994" in caplog.text
    assert ("[SKIP] 6994 — Oh! Sweet Nuthin’ (5.1 mix): its new recording names another song; check it by hand; "
            f"mb_trackid {SURROUND} left alone") in caplog.text


@needs_pg
def test_a_recorded_runs_apply_leaves_another_song_with_its_outcome(pg_url, monkeypatch, caplog):
    caplog.set_level("INFO")
    items = _library()
    lib = RunLib(items)
    _recorded_run(pg_url, monkeypatch, lib)
    monkeypatch.setattr(mbf, "musicbrainz_recordings", lambda: _recordings(ANOTHER_SONG, fails={LIVE}))
    counts = mbf.replace_run("r1", apply=True)
    assert counts["replaced"] == 0 and counts["skipped"] == 3 and (counts["suspect"], counts["norefresh"]) == (2, 1)
    by_id = {i.id: i for i in items}
    assert (by_id[6994]["mb_trackid"], by_id[6930]["mb_trackid"], by_id[5672]["mb_trackid"]) == (
        SURROUND, SURROUND, ALTERNATE)
    another, unasked = ("its new recording names another song; check it by hand",
                        "MusicBrainz couldn't be asked whether its new recording is the same song; run it again")
    assert _rows(pg_url, "SELECT item_id, outcome, outcome_reason FROM recheck_findings "
                         "WHERE replacement IS NOT NULL ORDER BY item_id") == [
        (5672, "skipped", unasked), (6930, "skipped", another), (6994, "skipped", another)]
    assert "not replaced: 1 whose MusicBrainz lookup failed (run it again), 2 [SUSPECT]" in caplog.text
    assert _events(pg_url) == []

    monkeypatch.setattr(mbf, "musicbrainz_recordings", lambda: _recordings())
    again = mbf.replace_run("r1", apply=True)  # each is tried again: now the recordings are the same songs
    assert again["replaced"] == 3 and by_id[5672]["mb_trackid"] == LIVE and by_id[6994]["mb_trackid"] == STEREO


# --- --refresh-replaced: the items replaced before the refresh existed ---


def _replaced(url, *pairs, at="2026-10-31T20:50:00Z"):
    """A recording_replaced event for each (item ID, new recording), as #2543's apply stored them."""
    from music_fetch import db, events

    with contextlib.closing(db.connect(url)) as conn:
        for item_id, new in pairs:
            events.insert(conn, mbf.EVENT_KIND, f"[RECHECK] {item_id}", source=mbf.SOURCE, item_id=item_id,
                          data={"old": "guess", "new": new})
        conn.execute("UPDATE events SET at = %s WHERE at > now() - interval '1 minute'", (at,))


def _past():
    """The library after #2543's apply: the IDs replaced, the guesses' fields left."""
    return [
        FakeItem(3778, "Constructive Summer", via="spotdl", mb_trackid=CONSTRUCTIVE["id"],
                 trackdisambig="live, 2024-03-10: Omeara, London, England"),
        FakeItem(6994, "Oh! Sweet Nuthin’ (5.1 mix)", via="usenet", mb_trackid=STEREO),
        FakeItem(5672, "The Big Country", via="usenet", mb_trackid=LIVE,
                 trackdisambig="live, 1979-08-27: Central Park, New York"),  # current already
        FakeItem(5970, "Purple Rain", mb_trackid="set-by-hand"),  # changed since its event
        FakeItem(6930, "Oh! Sweet Nuthin’ (5.1 mix)", mb_trackid="lookup-fails"),
    ]


@needs_pg
def test_refresh_replaced_lists_before_and_after_and_changes_nothing(pg_url, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "musicbrainz_recordings",
                        lambda: _recordings({**RECORDINGS, CONSTRUCTIVE["id"]: CONSTRUCTIVE}, fails={"lookup-fails"}))
    items = _past()
    _open(monkeypatch, RunLib(items))
    _replaced(pg_url, (3778, CONSTRUCTIVE["id"]), (6994, STEREO), (5672, LIVE), (5970, STUDIO),
              (6930, "lookup-fails"), (4242, "gone"))
    before = {i.id: dict(i.data) for i in items}

    counts = mbf.refresh_replaced()
    assert {i.id: i.data for i in items} == before and not any(i.stored or i.written for i in items)
    assert (counts["replaced"], counts["refresh"], counts["current"], counts["skipped"], counts["norefresh"]) == (
        6, 2, 1, 2, 1)
    assert ("[WOULD-REFRESH] 3778 — Constructive Summer: trackdisambig 'live, 2024-03-10: Omeara, London, "
            "England' → ''") in caplog.text
    assert "[WOULD-REFRESH] 6994 — Oh! Sweet Nuthin’ (5.1 mix): title 'Oh! Sweet Nuthin’ (5.1 mix)' → " \
           "'Oh! Sweet Nuthin’'" in caplog.text
    assert "[SKIP] 5970 — Purple Rain: mb_trackid is set-by-hand now, not its replacement studio-rec" in caplog.text
    assert "[SKIP] 4242: gone from the library" in caplog.text
    assert "[NOREFRESH] 6930" in caplog.text
    assert ("[SUMMARY] 6 replaced item(s): 2 to refresh (re-run with --apply), 1 current already, 2 skipped, "
            "1 not looked up (MusicBrainz lookup failed: 6930; run it again), 0 suspect") in caplog.text
    assert _events(pg_url)[-1][2] == 4242  # no event of its own


@needs_pg
def test_refresh_replaced_applies_under_the_lock_and_never_touches_mb_trackid(pg_url, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(mbf, "musicbrainz_recordings",
                        lambda: _recordings({**RECORDINGS, CONSTRUCTIVE["id"]: CONSTRUCTIVE}, fails={"lookup-fails"}))
    items = _past()
    _open(monkeypatch, RunLib(items))
    _replaced(pg_url, (3778, CONSTRUCTIVE["id"]), (6994, STEREO), (5970, STUDIO), (6930, "lookup-fails"))
    from music_scan import audit

    locks = []

    @contextlib.contextmanager
    def lock():
        locks.append("pipeline")
        yield

    monkeypatch.setattr(audit, "pipeline_lock", lock)
    counts = mbf.refresh_replaced(apply=True)
    assert counts["refreshed"] == 2 and locks == ["pipeline"]
    by_id = {i.id: i for i in items}
    assert by_id[3778]["trackdisambig"] == "" and by_id[3778].stored and by_id[3778].written
    assert by_id[6994]["title"] == "Oh! Sweet Nuthin’" and by_id[6994]["mb_trackid"] == STEREO
    assert by_id[5970]["mb_trackid"] == "set-by-hand" and not by_id[5970].stored
    assert by_id[6930]["title"] == "Oh! Sweet Nuthin’ (5.1 mix)" and not by_id[6930].stored
    assert "[REFRESH] 3778 — Constructive Summer: trackdisambig" in caplog.text
    # Each change is an event (homelab#2544), so album_history shows it, not just the exec's output.
    refreshed = [e for e in _events(pg_url) if e[0] == mbf.REFRESH_KIND]
    assert sorted(e[2] for e in refreshed) == [3778, 6994]
    wasp = next(e for e in refreshed if e[2] == 6994)
    assert wasp[1] == mbf.SOURCE and wasp[3].startswith("[REFRESH] 6994 — Oh! Sweet Nuthin’: title")
    assert wasp[4]["title"] == "Oh! Sweet Nuthin’ (5.1 mix)" and wasp[4]["recording"] == STEREO
    assert wasp[4]["refreshed"] == {"title": ["Oh! Sweet Nuthin’ (5.1 mix)", "Oh! Sweet Nuthin’"]}

    caplog.clear()
    again = mbf.refresh_replaced(apply=True)  # nothing new to change
    assert again["refreshed"] == 0 and again["current"] == 2
    assert len([e for e in _events(pg_url) if e[0] == mbf.REFRESH_KIND]) == 2


@needs_pg
def test_refresh_replaced_since_takes_only_later_replacements(pg_url, monkeypatch, musicbrainz):
    items = _past()
    _open(monkeypatch, RunLib(items))
    _replaced(pg_url, (6994, STEREO), at="2026-10-01T00:00:00Z")
    _replaced(pg_url, (5672, LIVE), at="2026-10-31T20:00:00Z")
    counts = mbf.refresh_replaced(since=mbf.utc_time("2026-10-31"))
    assert counts["replaced"] == 1 and musicbrainz.calls == [LIVE]


def test_refresh_replaced_refuses_without_the_database(monkeypatch):
    monkeypatch.delenv("MUSIC_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="MUSIC_DATABASE_URL is unset"):
        mbf.refresh_replaced()


def test_main_runs_refresh_replaced_with_its_own_flags_only(monkeypatch):
    calls = []
    monkeypatch.setattr(mbf, "refresh_replaced", lambda **kw: calls.append(kw))
    monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", "--refresh-replaced", "--since", "2026-10-31T20:00",
                                      "--apply"])
    mbf.main()
    assert calls == [{"since": mbf.utc_time("2026-10-31T20:00:00+00:00"), "apply": True}]
    for argv in (["--refresh-replaced", "--recheck"], ["--refresh-replaced", "6994"], ["--since", "2026-10-31"],
                 ["--refresh-replaced", "--since", "yesterday"]):
        monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", *argv])
        with pytest.raises(SystemExit):
            mbf.main()
    assert len(calls) == 1


# --- a recording set by hand (homelab#2583) ---

ROUGH = "b4b4719b-2d0c-4b00-90cf-fac3772bdf3d"
BYE = "dd51202e-05ac-4bc9-b597-e117f81c47f8"
OLD_BYE = "0e4a1c0b-7a0d-4a4e-9a51-1f2b3c4d5e6f"
CREDITED = {
    BYE: {"id": BYE, "title": "Bye", "disambiguation": "", "length": 113000,
          "artist-credit": [{"name": "Elliott Smith", "joinphrase": ""}]},
    OLD_BYE: {"id": BYE, "title": "Bye", "disambiguation": ""},  # merged into BYE on MusicBrainz
    STEREO: {"id": STEREO, "title": "Something Else", "disambiguation": ""},
}


def _bye(**data):
    return FakeItem(4230, "Bye", mb_trackid=ROUGH, trackdisambig="rough mix 2", via="usenet",
                    artist="Elliott Smith", length=113.26, **data)


def test_the_set_recording_dry_run_shows_the_recording_and_changes_nothing(caplog):
    caplog.set_level("INFO")
    item = _bye()
    assert mbf.set_recording_on(FakeLib([item]), 4230, BYE, _recordings(CREDITED)) is False
    assert item["mb_trackid"] == ROUGH and not item.stored and not item.written
    assert (f"[SETREC] 4230 — Bye (Elliott Smith, 113.3 s): mb_trackid {ROUGH} → {BYE}, 'Bye' by Elliott Smith "
            "(113.0 s)") in caplog.text
    assert "with it: trackdisambig 'rough mix 2' → ''" in caplog.text
    assert "Dry run — nothing set" in caplog.text and "[SUSPECT]" not in caplog.text


@pytest.mark.parametrize("lookup, match", [
    (_recordings({}), "MusicBrainz has no recording"),
    (_recordings(CREDITED, fails=(BYE,)), "lookup of .* failed .*run it again"),
])
def test_set_recording_refuses_an_unknown_recording_or_a_failed_lookup(lookup, match):
    item = _bye()
    with pytest.raises(SystemExit, match=match):
        mbf.set_recording_on(FakeLib([item]), 4230, BYE, lookup)
    with pytest.raises(SystemExit, match="no library item with id 1"):
        mbf.set_recording_on(FakeLib([item]), 1, BYE, _recordings(CREDITED))
    assert item["mb_trackid"] == ROUGH and not item.stored


def test_set_recording_refuses_a_bad_id_and_an_apply_without_the_database(monkeypatch):
    with pytest.raises(SystemExit, match="not a MusicBrainz recording ID"):
        mbf.set_recording(4230, "--apply", lookup=_recordings(CREDITED))
    monkeypatch.delenv("MUSIC_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="MUSIC_DATABASE_URL is unset"):
        mbf.set_recording(4230, BYE, apply=True, lookup=_recordings(CREDITED))


def test_a_recording_naming_another_song_is_suspect_in_the_dry_run(caplog):
    item = _bye()
    mbf.set_recording_on(FakeLib([item]), 4230, STEREO, _recordings(CREDITED))
    assert "[SUSPECT] 4230 — Bye: the recording is titled 'Something Else'" in caplog.text


def test_the_recheck_leaves_an_item_set_by_hand_alone():
    items = [_bye(recording_set=ROUGH), FakeItem(2, "Changed since", mb_trackid=SURROUND, recording_set=BYE)]
    lookup = _lookup({2: [(0.97, STEREO)]})
    counts, flagged = _recheck(items, lookup)
    assert lookup.calls == [2] and counts["set"] == 1 and [f.item_id for f in flagged] == [2]
    f = mbf.Flagged(4230, ROUGH, [(0.97, BYE)], keeper=False)
    assert mbf.verdict(items[0], f) == (mbf.SKIP, "its recording was set by hand (--set-recording)")


def test_main_takes_set_recording_with_apply_only(monkeypatch):
    calls = []
    monkeypatch.setattr(mbf, "set_recording", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", "--set-recording", "4230", BYE, "--apply"])
    mbf.main()
    assert calls == [((4230, BYE), {"apply": True})]
    for argv in (["--set-recording", "4230", BYE, "--recheck"], ["--set-recording", "x", BYE]):
        monkeypatch.setattr(sys, "argv", ["music-mb-fingerprint", *argv])
        with pytest.raises(SystemExit):
            mbf.main()
    assert len(calls) == 1


@needs_pg
def test_set_recording_stores_it_with_its_event_and_marks_the_item(pg_url, monkeypatch, caplog):
    from music_fetch import db

    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    caplog.set_level("INFO")
    item = _bye()
    lib = FakeLib([item])
    with contextlib.closing(db.connect()) as conn:
        # An old ID MusicBrainz merged: the recording it was merged into is set.
        assert mbf.set_recording_on(lib, 4230, OLD_BYE, _recordings(CREDITED), conn) is True
    assert (item["mb_trackid"], item["recording_set"], item["trackdisambig"]) == (BYE, BYE, "")
    assert item["title"] == "Bye" and item.stored and item.written
    assert f"[MERGED] MusicBrainz merged {OLD_BYE} into {BYE}" in caplog.text
    [(kind, source, item_id, message, data)] = _events(pg_url)
    assert (kind, source, item_id) == ("recording_set", "mb-fingerprint", 4230)
    assert message == f"[SETREC] 4230 — Bye: mb_trackid {ROUGH} → {BYE}, set by hand"
    assert data["old"] == ROUGH and data["new"] == BYE and data["refreshed"] == {"trackdisambig": ["rough mix 2", ""]}

    item.stored = item.written = False
    with contextlib.closing(db.connect()) as conn:  # again: nothing new, the tags written once more
        assert mbf.set_recording_on(lib, 4230, BYE, _recordings(CREDITED), conn) is False
    assert not item.stored and item.written and len(_events(pg_url)) == 1
    assert f"[DONE] 4230 — Bye: holds {BYE}, set by hand, already" in caplog.text


@needs_pg
def test_a_set_recording_that_fails_to_store_leaves_no_event(pg_url, monkeypatch):
    from music_fetch import db

    item = _bye()

    def fail():
        raise OSError("disk full")

    item.store = fail
    with contextlib.closing(db.connect()) as conn, pytest.raises(OSError):
        mbf.set_recording_on(FakeLib([item]), 4230, BYE, _recordings(CREDITED), conn)
    assert _events(pg_url) == [] and not item.written


# ---------------------------------------------------------------------------
# --set-recording aligns the ISRC and mb_releasetrackid (homelab#2623)
# ---------------------------------------------------------------------------

MB_RESPONSES = Path(__file__).resolve().parent / "mb_responses"
LIVE_TAKE = "4cb4128a-0e58-4b53-a04f-3551c7292f76"  # My Father's House, live 2025-04-22
STUDIO_2B = "ab727990-c905-4b2d-bcfd-f2f991c2d7ef"
GOLD_RUSH_STUDIO = "a1d14a11-b6ec-4dd9-a8f8-c58deb43a41f"


def _recorded_get(calls=None):
    """MusicBrainz over the responses recorded for homelab#2623; None (a 404) for any other."""
    import json

    def get(path, **params):
        if calls is not None:
            calls.append(path)
        kind, key = path.split("/")
        f = MB_RESPONSES / f"{kind}-{key if kind == 'isrc' else key[:8]}.json"
        return json.loads(f.read_text()) if f.exists() else None
    return get


def _fathers_house(**data):
    """Item 17061: the live take's recording and ISRC on the studio file, mb_releasetrackid the recording ID."""
    return FakeItem(17061, "My Father’s House", **{
        "mb_trackid": LIVE_TAKE, "mb_releasetrackid": LIVE_TAKE, "isrc": "USSM12503412",
        "mb_albumid": "ee7b8cb1-a1c6-41a8-8f5c-3bc9dd9b8d21", "disc": 4, "track": 9, "via": "usenet",
        "artist": "Bruce Springsteen", "length": 307.48, **data})


def _studio_lookup():
    import json

    recording = json.loads((MB_RESPONSES / "recording-ab727990.json").read_text())
    return lambda recording_id: recording if recording_id == STUDIO_2B else None


def _song(name, isrc, song_id="0000000000000000000000"):
    return {"name": name, "artists": ["Bruce Springsteen"], "isrc": isrc, "song_id": song_id}


def test_set_recording_drops_another_recordings_isrc_and_the_stale_release_track(caplog):
    """17061: the file's ISRC is the live take's on MusicBrainz, the studio take has none, and
    the recording isn't on the item's (Japanese) release: both fields are cleared."""
    caplog.set_level("INFO")
    item = _fathers_house()
    calls = []
    assert mbf.set_recording_on(FakeLib([item]), 17061, STUDIO_2B, _studio_lookup(), get=_recorded_get(calls),
                                songs={"soft": [_song("Reason To Believe", "USSM18200430")]}) is False
    assert calls == ["isrc/USSM12503412", "release/ee7b8cb1-a1c6-41a8-8f5c-3bc9dd9b8d21"]
    assert f"[ISRC] USSM12503412 goes: MusicBrainz gives it to {LIVE_TAKE}" in caplog.text
    assert "[RELEASETRACK] none: the recording isn't on the item's release" in caplog.text
    assert f"isrc 'USSM12503412' → ''; mb_releasetrackid '{LIVE_TAKE}' → ''" in caplog.text
    assert item["isrc"] == "USSM12503412" and not item.stored  # a dry run


def test_set_recording_refuses_to_empty_a_playlist_slot_held_by_that_isrc():
    item = _fathers_house()
    songs = {"soft": [_song("My Father’s House", "USSM12503412")]}
    with pytest.raises(SystemExit, match=r"would empty the slot of soft: My Father’s House — Bruce Springsteen "
                                         r"\(ISRC USSM12503412\)"):
        mbf.set_recording_on(FakeLib([item]), 17061, STUDIO_2B, _studio_lookup(), get=_recorded_get(), songs=songs)
    # Another item holds the ISRC too, or the entry resolves by its Spotify ID: no slot empties.
    other = FakeItem(17000, "My Father’s House (live)", isrc="USSM12503412")
    mbf.set_recording_on(FakeLib([item, other]), 17061, STUDIO_2B, _studio_lookup(), get=_recorded_get(),
                         songs=songs)
    held = FakeItem(17001, "Other", spotify_ids="5pKCCKE2ajJHZ9KAiaK11H")
    mbf.set_recording_on(FakeLib([item, held]), 17061, STUDIO_2B, _studio_lookup(), get=_recorded_get(),
                         songs={"soft": [_song("My Father’s House", "USSM12503412", "5pKCCKE2ajJHZ9KAiaK11H")]})
    # Without the playlist stores nothing tells: refused.
    with pytest.raises(SystemExit, match="playlists couldn't be checked"):
        mbf.set_recording_on(FakeLib([item]), 17061, STUDIO_2B, _studio_lookup(), get=_recorded_get(), songs=None)


def test_set_recording_takes_the_release_track_and_keeps_an_isrc_the_recording_has(caplog):
    """3809: the studio recording is at disc 1 #2 of the item's release; its ISRC there is kept,
    and one MusicBrainz doesn't know stays (it may be Spotify's)."""
    caplog.set_level("INFO")
    item = FakeItem(3809, "After the Gold Rush", mb_trackid="0ef25c6b-c082-4750-8b02-e27fe7c121a3",
                    mb_releasetrackid="0ef25c6b-c082-4750-8b02-e27fe7c121a3", isrc="USRE10900209;XX0000000001",
                    mb_albumid="67101825-7368-4d64-a081-f35e294bde5b", disc=1, track=2, artist="Neil Young",
                    length=226.89)
    recording = {"id": GOLD_RUSH_STUDIO, "title": "After the Gold Rush", "disambiguation": "original studio mix",
                 "isrcs": ["USRE10900209", "USRE10900619", "USRE11700210", "USRE19900251"]}
    mbf.set_recording_on(FakeLib([item]), 3809, GOLD_RUSH_STUDIO, lambda _: recording, get=_recorded_get(),
                         songs={})
    assert "[ISRC] XX0000000001 stays: MusicBrainz knows no recording with it" in caplog.text
    assert "isrc '" not in caplog.text  # unchanged
    assert ("mb_releasetrackid '0ef25c6b-c082-4750-8b02-e27fe7c121a3' → "
            "'d9edc8b8-") in caplog.text


def test_set_recording_sets_the_recordings_only_isrc():
    item = _fathers_house(isrc="")
    recording = {"id": STUDIO_2B, "title": "My Father’s House", "disambiguation": "", "isrcs": ["USSM18200429"]}
    changes = mbf._aligned(FakeLib([item]), item, recording, STUDIO_2B, _recorded_get(), {})
    assert changes["isrc"] == ("", "USSM18200429")


@needs_pg
def test_set_recording_stores_the_aligned_fields_with_its_event(pg_url, monkeypatch):
    from music_fetch import db

    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    item = _fathers_house()
    with contextlib.closing(db.connect()) as conn:
        assert mbf.set_recording_on(FakeLib([item]), 17061, STUDIO_2B, _studio_lookup(), conn,
                                    get=_recorded_get(), songs={}) is True
    assert (item["mb_trackid"], item["isrc"], item["mb_releasetrackid"]) == (STUDIO_2B, "", "")
    assert item["trackdisambig"] == "version 2b" and item.stored and item.written
    [(kind, _, _, _, data)] = _events(pg_url)
    assert kind == "recording_set" and (data["old"], data["new"]) == (LIVE_TAKE, STUDIO_2B)
    assert data["refreshed"] == {"trackdisambig": ["", "version 2b"], "isrc": ["USSM12503412", ""],
                                 "mb_releasetrackid": [LIVE_TAKE, ""]}

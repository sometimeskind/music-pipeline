"""music-mb-fingerprint --recheck (homelab#2543): a guessed mb_trackid checked against AcoustID.

The cases are the library's: a stereo track beets matched to the 5.1 mix (6994,
and 6930, a merge keeper), a live album's track matched to the studio alternate
version (5672), and a studio track matched to a live recording (5970).  The
replacement tests run against Postgres when ``MUSIC_TEST_DATABASE_URL`` is set
(fetch/tests/pg_fixture.py).
"""

from __future__ import annotations

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
    counts, flagged = mbf.recheck(_library(), _lookup())
    assert {f.item_id: f.replacement for f in flagged} == {6994: STEREO, 6930: STEREO, 5672: LIVE, 5970: None}
    assert counts["checked"] == 4 and counts["flagged"] == 4 and counts["agree"] == 0
    # 6930 is a keeper and 5970 has two recordings: only 6994 and 5672 are replaced without --keepers.
    assert counts["replaceable"] == 2 and counts["keepers"] == 1 and counts["ambiguous"] == 1
    assert ("[RECHECK] 6994 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec is not among its "
            "AcoustID recordings (stereo-rec 0.97): → stereo-rec") in caplog.text
    assert "6930 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec is not among" in caplog.text
    assert "a merge keeper, left alone without --keepers (→ stereo-rec)" in caplog.text
    assert "[RECHECK] 5672 — The Big Country (alternate version): mb_trackid alternate-rec" in caplog.text
    assert ("[RECHECK] 5970 — Purple Rain: mb_trackid live-1992-rec is not among its AcoustID recordings "
            "(studio-rec 0.98): 2 recordings (studio-rec 0.98, remaster-rec 0.91), none chosen; left alone"
            ) in caplog.text


def test_keepers_makes_a_keeper_replaceable():
    counts, _ = mbf.recheck(_library(), _lookup(), keepers=True)
    assert counts["replaceable"] == 3 and counts["keepers"] == 1


def test_a_guess_among_its_recordings_is_not_reported(caplog):
    caplog.set_level("INFO")
    # AcoustID links the fingerprint to the stereo and the 5.1 recording: the guess stands.
    counts, flagged = mbf.recheck(_library()[:1], _lookup({6994: [(0.97, STEREO), (0.90, SURROUND)]}))
    assert flagged == [] and counts["agree"] == 1 and caplog.text == ""


@pytest.mark.parametrize("found,best", [([], "no recording"), ([(0.6, STEREO)], "best stereo-rec 0.60")])
def test_nothing_confident_is_nomatch_and_left_alone(caplog, found, best):
    caplog.set_level("INFO")
    counts, flagged = mbf.recheck(_library()[:1], _lookup({6994: found}))
    assert flagged == [] and counts["nomatch"] == 1
    assert (f"[NOMATCH] 6994 — Oh! Sweet Nuthin’ (5.1 mix): AcoustID has nothing at 0.85 or more ({best}); "
            "mb_trackid surround-rec left alone") in caplog.text


def test_a_request_fallback_and_an_item_without_an_id_are_not_looked_up():
    items = [FakeItem(1, "Fallback", mb_trackid=STEREO, fallback_recording=STEREO), FakeItem(2, "No ID")]
    lookup = _lookup({})
    counts, flagged = mbf.recheck(items, lookup)
    assert lookup.calls == [] and flagged == [] and counts["skipped"] == 1 and counts["checked"] == 0


def test_a_failed_lookup_is_retried_once_then_an_error(monkeypatch):
    monkeypatch.setattr(mbf, "RETRY_SECONDS", 0)
    tries = []

    def flaky(path):
        tries.append(path)
        if len(tries) == 1:
            raise mbf.LookupFailed("503")
        return [(0.97, STEREO)]

    counts, flagged = mbf.recheck(_library()[:1], flaky)
    assert len(tries) == 2 and counts["errors"] == 0 and [f.replacement for f in flagged] == [STEREO]

    def down(path):
        raise mbf.LookupFailed("503")

    counts, _ = mbf.recheck(_library()[:1], down)
    assert counts["errors"] == 1 and counts["checked"] == 0


def test_lookups_failing_in_a_row_stop_the_run(monkeypatch):
    monkeypatch.setattr(mbf, "RETRY_SECONDS", 0)
    monkeypatch.setattr(mbf, "MAX_FAILURES_IN_A_ROW", 3)

    def down(path):
        raise mbf.LookupFailed("invalid API key")

    items = [FakeItem(n, f"T{n}", mb_trackid=STEREO) for n in range(1, 6)]
    with pytest.raises(SystemExit, match="3 AcoustID lookups failed in a row"):
        mbf.recheck(items, down)


def test_a_file_without_a_fingerprint_is_an_error_not_a_failure_in_a_row(monkeypatch):
    monkeypatch.setattr(mbf, "MAX_FAILURES_IN_A_ROW", 2)

    def unreadable(path):
        raise ValueError("no fingerprint: decode error")

    items = [FakeItem(n, f"T{n}", mb_trackid=STEREO) for n in range(1, 6)]
    counts, _ = mbf.recheck(items, unreadable)
    assert counts["errors"] == 5


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
    _, flagged = mbf.recheck(items, _lookup())
    assert mbf.replace(FakeLib(items), flagged) == 2
    by_id = {i.id: i for i in items}
    assert by_id[6994]["mb_trackid"] == STEREO and by_id[6994].stored and by_id[6994].written
    assert by_id[6994]["mb_releasetrackid"] == SURROUND  # kept as it was
    assert by_id[5672]["mb_trackid"] == LIVE
    assert by_id[6930]["mb_trackid"] == SURROUND and not by_id[6930].stored  # a keeper
    assert by_id[5970]["mb_trackid"] == LIVE_1992 and not by_id[5970].stored  # two recordings
    rows = _events(pg_url)
    assert [(k, s, i) for k, s, i, _, _ in rows] == [("recording_replaced", "mb-fingerprint", 6994),
                                                     ("recording_replaced", "mb-fingerprint", 5672)]
    assert rows[0][3] == ("[RECHECK] 6994 — Oh! Sweet Nuthin’ (5.1 mix): mb_trackid surround-rec → stereo-rec "
                          "(AcoustID 0.97)")
    assert rows[0][4]["old"] == SURROUND and rows[0][4]["new"] == STEREO and rows[0][4]["keeper"] is False


@needs_pg
def test_replace_with_keepers_replaces_a_keeper(pg_url, monkeypatch):
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    items = _library()
    _, flagged = mbf.recheck(items, _lookup(), keepers=True)
    assert mbf.replace(FakeLib(items), flagged, keepers=True) == 3
    assert items[1]["mb_trackid"] == STEREO
    assert [r[4]["keeper"] for r in _events(pg_url)] == [False, True, False]


@needs_pg
def test_replace_skips_an_item_changed_since_its_lookup(pg_url, monkeypatch, caplog):
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    items = _library()
    _, flagged = mbf.recheck(items, _lookup())
    items[0]["mb_trackid"] = "set-by-hand"
    lib = FakeLib(items)
    del lib.by_id[5672]  # it left the library meanwhile
    assert mbf.replace(lib, flagged) == 0
    assert items[0]["mb_trackid"] == "set-by-hand" and _events(pg_url) == []
    assert "[SKIP] 6994: mb_trackid is set-by-hand now, not surround-rec" in caplog.text
    assert "[SKIP] 5672: mb_trackid is (gone from the library) now" in caplog.text


@needs_pg
def test_a_store_that_fails_leaves_no_event(pg_url, monkeypatch):
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    items = _library()
    _, flagged = mbf.recheck(items, _lookup())

    def locked():
        raise RuntimeError("database is locked")

    items[0].store = locked
    with pytest.raises(RuntimeError, match="database is locked"):
        mbf.replace(FakeLib(items), flagged)
    assert _events(pg_url) == []

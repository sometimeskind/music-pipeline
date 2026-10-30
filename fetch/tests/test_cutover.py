"""The stage 3 cutover (homelab#2478, ``music-cutover``): Later onto the queue in
its store's order, Keep into requests, and the revert."""

from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from music_fetch import album_requests, albums, later_queue, state, store
from music_scan import cutover

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture
from mb_fake import OTHER_RG, RG, FakeMB  # noqa: E402

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
RG_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
RG_C = "cccccccc-3333-4333-8333-cccccccccccc"
RG_ASKED = "dddddddd-4444-4444-8444-dddddddddddd"


def _song(album_id: str, n: int = 1) -> dict:
    return {"name": f"Song {n}", "artists": ["Band"], "song_id": f"{album_id}-{n}", "album_id": album_id,
            "album_name": f"Album {album_id}", "album_artist": "Band", "isrc": None}


def _record(album_id: str, group: str | None, status: str = albums.HAVE, playlist: str = "later") -> dict:
    return {"status": status, "release_group": group, "unresolved": None if group else state.NO_MATCH,
            "spotify_album_ids": [album_id], "names": [], "name": f"Album {album_id}", "artist": "Band",
            "tracks_count": 1, "blocklist": [], "playlists": {playlist: [["Song 1", "Band", f"{album_id}-1", None, 1, 1]]}}


# ---------------------------------------------------------------------------
# The plan and the check, no database
# ---------------------------------------------------------------------------


def test_the_plan_takes_later_in_store_order_and_splits_keep():
    rows = state.AlbumRows(albums={
        "A": _record("A", RG_A), "B": _record("B", RG_B, albums.WANTED), "N": _record("N", None),
        "K": _record("K", RG_C, playlist="keep"), "Q": _record("Q", RG_ASKED, playlist="keep"),
        "U": _record("U", None, playlist="keep")})
    later = [_song("B"), _song("A"), _song("B", 2), _song("N"), _song("GONE")]
    p = cutover.plan(rows, later, [_song("K"), _song("Q"), _song("U")], requested=[RG_ASKED])
    assert [k for k, _ in p.later] == ["B", "A"]  # first appearance, each once
    assert [k for k, _ in p.unresolved["later"]] == ["N"]
    assert p.unknown["later"] == ["Band — Album GONE (GONE)"]
    assert [k for k, _ in p.keep] == ["K"]
    assert [k for k, _ in p.requested] == ["Q"]
    assert [k for k, _ in p.unresolved["keep"]] == ["U"]
    text = "\n".join(cutover.lines(p, {}))
    assert "2 album(s) to queue" in text and "1 available now, 1 pending" in text
    assert "[NO-RG] later: Band — Album N (album N): have, no-match" in text
    assert "[NO-ROW] later: Band — Album GONE (GONE)" in text


def test_a_twin_edition_is_one_album():
    record = _record("A", RG_A)
    record["spotify_album_ids"] = ["A", "A2"]
    p = cutover.plan(state.AlbumRows(albums={"A": record}), [_song("A2"), _song("A")], [], [])
    assert [k for k, _ in p.later] == ["A"]


def test_the_check_says_unchanged_or_lists_the_difference():
    assert cutover.compare(["a", "b"], ["a", "b"]) == ["later.m3u: unchanged by the cutover (2 line(s))"]
    assert cutover.compare(None, ["a"]) == ["later.m3u: none yet; the queue writes 1 line(s)"]
    out = cutover.compare(["a", "b", "c"], ["a", "d"])
    assert out[0].startswith("later.m3u: CHANGED by the cutover: 3 line(s) now, 2 after; 2 only now, 1 only after")
    assert out[1:] == ["  - b", "  - c", "  + d"]
    assert cutover.compare(["a", "b"], ["b", "a"])[0].endswith("the same lines in another order")


# ---------------------------------------------------------------------------
# --apply and --revert, against the database
# ---------------------------------------------------------------------------


@pytest.fixture
def switched(tmp_path: Path, pg_url, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")
    monkeypatch.setattr(state, "SWITCH", tmp_path / ".state-in-db")
    (tmp_path / ".state-in-db").touch()
    # The check reads beets and the .m3u on disk; its parts are tested above.
    monkeypatch.setattr(cutover, "m3u_lines", lambda found: [k for k, _ in found])
    monkeypatch.setattr(cutover, "current_m3u", lambda: None)
    return pg_url


def _setup(later_songs: list[dict], keep_songs: list[dict], *records: dict) -> None:
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums={r["spotify_album_ids"][0]: r for r in records}))
    store.save(store.Store.from_songs("later", later_songs))
    store.save(store.Store.from_songs("keep", keep_songs))


def _queue() -> list[tuple[str, bool]]:
    with state.connect() as conn:
        return [(e.release_group, e.available_at is not None) for e in later_queue.load(conn)]


def _asked_on_later() -> None:
    """An album requested with later before the cutover, and held: on the queue, available."""
    with state.connect() as conn:
        conn.execute("INSERT INTO later_queue (release_group, available_at) VALUES (%s, now())", (RG_ASKED,))


def _run(caplog, *, apply: bool = True, revert: bool = False, get=None) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=cutover.__name__), state.connect() as conn:
        if revert:
            cutover.revert(conn, apply)
        else:
            cutover.cut_over(conn, apply, get=get or FakeMB())
    return [r.getMessage() for r in caplog.records if r.name == cutover.__name__]


@needs_pg
def test_apply_queues_later_ahead_of_the_queue_and_requests_keep(switched, caplog):
    _setup([_song("B"), _song("A")], [_song("K"), _song("X")],
           _record("A", RG_A), _record("B", RG_B, albums.WANTED), _record("ASK", RG_ASKED, albums.IMPORTED),
           _record("K", RG, playlist="keep"), _record("X", OTHER_RG, playlist="keep"))
    _asked_on_later()

    dry = _run(caplog, apply=False)
    assert dry[-1] == "Dry run; --apply to write"
    assert _queue() == [(RG_ASKED, True)]

    out = _run(caplog)
    # B before A (the store's order), both before the album requested since; B pending until held.
    assert _queue() == [(RG_A, True), (RG_ASKED, True), (RG_B, False)]
    with state.connect() as conn:
        entries = {e.release_group: e for e in later_queue.load(conn)}
        assert entries[RG_B].added_at < entries[RG_A].added_at < entries[RG_ASKED].added_at
        assert entries[RG_A].available_at < entries[RG_ASKED].available_at
        requests = album_requests.load(conn)
        marker = state.get(conn, state.CUTOVER)
        kinds = [k for (k,) in conn.execute("SELECT kind FROM events WHERE source = 'cutover' ORDER BY id")]
    assert [(r.release_group, r.requested_by) for r in requests] == [(RG, "cutover:keep")]
    assert marker["queue"] == {RG_B: None, RG_A: None} and marker["requests"] == [RG]
    assert kinds == ["later_added", "later_added"]
    assert any(line.startswith("  [SKIP] keep: Band — Album X") for line in out)  # MusicBrainz has no OTHER_RG
    assert out[-1] == ("Applied: 2 album(s) on Later in the later store's order, 1 Keep request(s) added, "
                       "1 skipped (--apply again for them)")

    # B, pending at the cutover, joins the end once held, as any arrival does.
    with state.connect() as conn:
        rows = state.load_albums(conn)
        next(r for r in rows.albums.values() if r["release_group"] == RG_B)["status"] = albums.IMPORTED
        state.save_albums(conn, rows)
        later_queue.mark_available(conn, state.load_albums(conn))
        found = later_queue.albums(conn, state.load_albums(conn))
    assert [r["release_group"] for _, r in found] == [RG_A, RG_ASKED, RG_B]  # mark_available stamps now()


@needs_pg
def test_apply_again_adds_only_what_is_missing_and_revert_takes_it_all_off(switched, caplog):
    _setup([_song("A"), _song("ASK")], [_song("K")],
           _record("A", RG_A), _record("ASK", RG_ASKED, albums.IMPORTED), _record("K", RG, playlist="keep"))
    _asked_on_later()
    with state.connect() as conn:
        (before,) = conn.execute("SELECT added_at, available_at FROM later_queue").fetchall()

    _run(caplog)
    assert _queue() == [(RG_A, True), (RG_ASKED, True)]  # the requested album moved to its store place
    out = _run(caplog)
    assert out[-1] == "Applied: 2 album(s) on Later in the later store's order, 0 Keep request(s) added"
    with state.connect() as conn:
        marker = state.get(conn, state.CUTOVER)
        assert len(album_requests.load(conn)) == 1
    assert marker["queue"][RG_A] is None and marker["queue"][RG_ASKED] is not None  # its first place, kept

    assert _run(caplog, apply=False, revert=True)[0] == \
        "Revert: 1 Later entr(ies) to remove, 1 to move back, 1 Keep request(s) to remove"
    out = _run(caplog, revert=True)
    assert out[-1] == "Reverted: 1 Later entr(ies) removed, 1 moved back, 1 Keep request(s) removed"
    with state.connect() as conn:
        assert conn.execute("SELECT added_at, available_at FROM later_queue").fetchall() == [before]
        assert album_requests.load(conn) == []
        assert state.get(conn, state.CUTOVER) is None
        assert [k for (k,) in conn.execute("SELECT kind FROM events WHERE kind = 'later_removed'")] == ["later_removed"]
    assert _run(caplog, revert=True) == [f"No cutover to revert (no {state.CUTOVER} marker)"]


@needs_pg
def test_without_a_store_it_refuses(switched, caplog):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums={"A": _record("A", RG_A)}))
    store.save(store.Store.from_songs("later", [_song("A")]))
    with pytest.raises(SystemExit, match="keep store can't be read"):
        _run(caplog, apply=False)


def test_queue_times_are_a_second_apart_and_before_the_first():
    class Conn:
        def execute(self, *_):
            return self

        def fetchone(self):
            from datetime import datetime, timezone
            return (datetime(2026, 10, 30, tzinfo=timezone.utc),)

    times = cutover.queue_times(Conn(), 3)
    assert [t.second for t in times] == [57, 58, 59]
    assert times[1] - times[0] == timedelta(seconds=1)

"""The Later queue (homelab#2478 stage 2b): music-requests add --later, the
queue's arrival order, and music-later's listing and removal."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from music_fetch import album_requests_cli, albums, later_cli, later_queue, state, store

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture
from mb_fake import RG, FakeMB  # noqa: E402

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG2 = "0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10"
RG3 = "7c3f5a2e-1b4d-4e6f-8a9b-0c1d2e3f4a5b"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")
    monkeypatch.setattr(state, "SWITCH", tmp_path / ".state-in-db")


@pytest.fixture
def switched(tmp_path: Path, pg_url, monkeypatch):
    (tmp_path / ".state-in-db").touch()
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "sub-tom")
    return pg_url


def _run(module, argv: list[str], caplog, **kw) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=module.__name__):
        module.run(module._parser().parse_args(argv), **kw)
    return [r.getMessage() for r in caplog.records if r.name == module.__name__]


def _rows(*records: dict) -> None:
    """Album rows, each with a release group, saved as the album tick would."""
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums={r["release_group"]: r for r in records}))


def _album(group: str, status: str, name: str = "Album") -> dict:
    return {"status": status, "release_group": group, "unresolved": None, "spotify_album_ids": [], "names": [],
            "origin": albums.REQUEST, "name": name, "artist": "Band", "tracks_count": 1, "blocklist": [],
            "playlists": {"request": [["One", "Band", None, "ISRC1", 1, 1]]}}


def _queue() -> list[later_queue.Entry]:
    with state.connect() as conn:
        return later_queue.load(conn)


def _held(group: str) -> None:
    """The album of *group* imported: its row held whole."""
    with state.connect() as conn:
        rows = state.load_albums(conn)
        [record] = [r for r in rows.albums.values() if r["release_group"] == group]
        record["status"] = albums.IMPORTED
        state.save_albums(conn, rows)


def _available() -> list[str]:
    with state.connect() as conn:
        later_queue.mark_available(conn, state.load_albums(conn))
        return [e.release_group for e in later_queue.load(conn) if e.available_at is not None]


@needs_pg
def test_a_request_with_later_is_appended_and_pending_until_held(switched, caplog):
    mb = FakeMB()
    dry = _run(album_requests_cli, ["add", "--later", RG], caplog, get=mb)
    assert dry == _run(album_requests_cli, ["add", "--later", RG], caplog, get=mb)
    assert "  Later: appended; it joins the end of Later once the library holds it whole" in dry
    assert "1 album(s) to append to Later, in this order" in dry
    assert _queue() == []
    applied = _run(album_requests_cli, ["add", "--later", "--apply", RG], caplog, get=mb)
    assert applied[-1] == "Applied: 1 request(s) added; 1 album(s) appended to Later"
    [entry] = _queue()
    assert entry.release_group == RG and entry.available_at is None
    with state.connect() as conn:
        kinds = [k for (k,) in conn.execute("SELECT kind FROM events ORDER BY id")]
        assert kinds == ["album_requested", "later_added"]
        caller, data = conn.execute("SELECT caller, data FROM events WHERE kind = 'later_added'").fetchone()
        assert caller == "sub-tom" and data["available"] is False and data["album"] == "Band & Friend — The Album"
    again = _run(album_requests_cli, ["add", "--later", RG], caplog, get=mb)
    assert "requested already" in again[0] and again[1] == "  Later: on it already, pending its import"
    assert not any("to append" in line for line in again)


@needs_pg
def test_without_later_nothing_is_queued(switched, caplog):
    applied = _run(album_requests_cli, ["add", "--apply", RG], caplog, get=FakeMB())
    assert applied[-1] == "Applied: 1 request(s) added"
    assert _queue() == []


@needs_pg
def test_an_album_requested_already_is_only_appended(switched, caplog):
    mb = FakeMB()
    _run(album_requests_cli, ["add", "--apply", RG], caplog, get=mb)
    applied = _run(album_requests_cli, ["add", "--later", "--apply", RG], caplog, get=mb)
    assert applied[-1] == "Applied: 0 request(s) added; 1 album(s) appended to Later"
    assert [e.release_group for e in _queue()] == [RG]


@needs_pg
def test_an_album_held_already_is_available_at_once(switched, caplog):
    _rows(_album(RG, albums.HAVE))
    dry = _run(album_requests_cli, ["add", "--later", RG], caplog, get=FakeMB())
    assert "  Later: appended; the library holds it, so it joins the end of Later at the next scan" in dry
    _run(album_requests_cli, ["add", "--later", "--apply", RG], caplog, get=FakeMB())
    [entry] = _queue()
    assert entry.available_at is not None


@needs_pg
def test_later_lists_albums_in_arrival_order_not_request_order(switched):
    """homelab#2478: an album that downloads late lands at the end, not behind the listening position."""
    _rows(_album(RG, albums.WANTED, "First"), _album(RG2, albums.WANTED, "Second"), _album(RG3, albums.HAVE, "Third"))
    with state.connect() as conn, conn.transaction():
        rows = state.load_albums(conn)
        assert later_queue.add(conn, [RG, RG2, RG3], rows, {}) == 3
        assert later_queue.add(conn, [RG], rows, {}) == 0  # on it already: stays where it is
    assert [e.release_group for e in _queue() if e.available_at] == [RG3]
    _held(RG2)
    assert _available() == [RG3, RG2]
    _held(RG)
    assert _available() == [RG3, RG2, RG]
    with state.connect() as conn:
        rows = state.load_albums(conn)
        assert [k for k, _ in later_queue.albums(conn, rows)] == [
            later_queue.by_group(rows)[g][0] for g in (RG3, RG2, RG)]
        listed = later_queue.listing(conn, rows)
    assert [(e["position"], e["state"], e["name"]) for e in listed] == [
        (1, "available", "Third"), (2, "available", "Second"), (3, "available", "First")]


@needs_pg
def test_a_reset_doesnt_move_an_album(switched):
    _rows(_album(RG, albums.HAVE), _album(RG2, albums.HAVE))
    with state.connect() as conn, conn.transaction():
        later_queue.add(conn, [RG, RG2], state.load_albums(conn), {})
    with state.connect() as conn:
        rows = state.load_albums(conn)
        for record in rows.albums.values():
            record["status"] = albums.WANTED
        state.save_albums(conn, rows)
    _held(RG)
    assert _available() == [RG, RG2]


@needs_pg
def test_a_pending_album_is_listed_after_the_available_ones(switched):
    _rows(_album(RG, albums.WANTED), _album(RG2, albums.IMPORTED))
    with state.connect() as conn, conn.transaction():
        later_queue.add(conn, [RG, RG2], state.load_albums(conn), {})
    with state.connect() as conn:
        listed = later_queue.listing(conn, state.load_albums(conn))
    assert [(e["release_group"], e["position"], e["state"], e["status"]) for e in listed] == [
        (RG2, 1, "available", albums.IMPORTED), (RG, None, "pending", albums.WANTED)]


@needs_pg
def test_music_later_removes_by_release_group_and_through(switched, caplog):
    _rows(_album(RG, albums.HAVE, "First"), _album(RG2, albums.HAVE, "Second"), _album(RG3, albums.WANTED, "Third"))
    with state.connect() as conn, conn.transaction():
        later_queue.add(conn, [RG, RG2, RG3], state.load_albums(conn), {})
    listed = _run(later_cli, ["list"], caplog)
    assert listed[0].startswith(f"1. Band — First [release group {RG}]: available; album ")
    assert listed[2].startswith(f"- Band — Third [release group {RG3}]: pending;")
    assert listed[-1] == "3 album(s) on Later, 1 pending"
    other = "11111111-2222-4333-8444-555555555555"
    dry = _run(later_cli, ["remove", "--through", "1", "--", RG3, other], caplog)
    assert dry == _run(later_cli, ["remove", "--through", "1", "--", RG3, other], caplog)
    assert dry == [
        f"Release group {other}: not on Later; nothing changes",
        f"Remove 1 of 2: Band — First [release group {RG}]: position 1 of 2",
        f"Remove 2 of 2: Band — Third [release group {RG3}]: pending: not in the .m3u yet",
        "2 album(s) leave Later at the next scan's regen; their files stay in the library",
        "Dry run; --apply to write"]
    assert len(_queue()) == 3
    applied = _run(later_cli, ["remove", "--apply", "--through", "1", "--", RG3, other], caplog)
    assert applied[-1] == "Applied: 2 album(s) removed from Later"
    assert [e.release_group for e in _queue()] == [RG2]
    with state.connect() as conn:
        removed = [g for (g,) in conn.execute(
            "SELECT release_group::text FROM events WHERE kind = 'later_removed' ORDER BY id")]
        assert removed == [RG, RG3]
        assert len(state.load_albums(conn).albums) == 3  # leaving Later touches no album


@needs_pg
def test_music_later_refuses_a_through_past_the_end_and_an_empty_call(switched, caplog):
    _rows(_album(RG, albums.HAVE))
    with state.connect() as conn, conn.transaction():
        later_queue.add(conn, [RG], state.load_albums(conn), {})
    with pytest.raises(later_cli.req.Refused, match="Later lists 1 album"):
        _run(later_cli, ["remove", "--through", "2"], caplog)
    with pytest.raises(later_cli.req.Refused, match="--through"):
        _run(later_cli, ["remove"], caplog)
    with pytest.raises(later_cli.req.Refused, match="1 or more"):
        _run(later_cli, ["remove", "--through", "0"], caplog)
    with pytest.raises(later_cli.req.Refused, match="not a MusicBrainz"):
        _run(later_cli, ["remove", "--", "nonsense"], caplog)


def test_music_later_refuses_before_the_state_is_in_the_database(caplog):
    with pytest.raises(later_cli.req.Refused, match="music-state-import"):
        _run(later_cli, ["list"], caplog)

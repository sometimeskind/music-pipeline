"""The pipeline's state in the music database (homelab#2410).

The database tests run against a real Postgres when ``MUSIC_TEST_DATABASE_URL``
is set (pg_fixture's ``pg_url``); the rest need none.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
import pytest

from music_fetch import albums, db, state

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"
RG2 = "0b4a7c4e-5d2f-4a51-8b1e-9a3c2d1e0f42"


def _records() -> dict[str, dict]:
    return {
        RG: {"status": "imported", "release_group": RG, "unresolved": None,
             "spotify_album_ids": ["sp1", "sp2"], "names": [], "name": "Album", "artist": "Artist",
             "blocklist": ["r1"], "playlists": {"later": [["t", "a", "s1", "ISRC1", 1, 1]]}},
        "sp3": {"status": "wanted", "release_group": None, "unresolved": "no-match",
                "spotify_album_ids": ["sp3"], "names": [], "origin": "library", "name": "Other",
                "artist": "Someone", "blocklist": []},
        "local/files": {"status": "have", "release_group": None, "unresolved": "pending",
                        "spotify_album_ids": [], "names": ["local/files"], "name": "files", "artist": "local"},
    }


def _strip(rows: state.AlbumRows) -> dict[str, dict]:
    return {k: {f: v for f, v in r.items() if f != "_id"} for k, r in rows.albums.items()}


# --- no database ---


def test_album_key_prefers_release_group_then_spotify_then_name():
    assert state.album_key({"release_group": RG, "spotify_album_ids": ["sp"]}) == RG
    assert state.album_key({"release_group": None, "spotify_album_ids": ["sp", "sq"], "names": ["n"]}) == "sp"
    assert state.album_key({"spotify_album_ids": [], "names": ["a/b"]}) == "a/b"
    with pytest.raises(ValueError):
        state.album_key({"status": "wanted"})


def test_connect_unset_is_unavailable(monkeypatch):
    monkeypatch.delenv(db.ENV, raising=False)
    with pytest.raises(state.Unavailable, match="unset"):
        state.connect()


def test_connect_down_is_unavailable(monkeypatch):
    def refuse(*a, **k):
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(db.psycopg, "connect", refuse)
    with pytest.raises(state.Unavailable, match="unreachable"):
        state.connect("postgresql://nowhere.invalid/music")


def test_unknown_kv_name_is_refused():
    with pytest.raises(ValueError):
        state.get(None, "albums")  # type: ignore[arg-type]


# --- against Postgres ---


def _check_values(conn, constraint: str) -> set[str]:
    row = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
                       (constraint,)).fetchone()
    return set(re.findall(r"'([^']+)'", row[0]))


@needs_pg
def test_checks_agree_with_album_mode(pg_url):
    with state.connect() as conn:
        assert _check_values(conn, "albums_status_check") == {
            albums.WANTED, albums.DRY_RUN, albums.GRABBED, albums.MISSING, albums.HAVE, albums.IMPORTED,
            albums.FAILED, albums.FALLBACK, albums.FILLED}
        assert _check_values(conn, "albums_origin_check") == {albums.ALBUM, albums.WANTS, albums.LIBRARY}
        assert _check_values(conn, "albums_unresolved_check") == set(state.UNRESOLVED)


@needs_pg
def test_albums_round_trip(pg_url):
    rows = state.AlbumRows(
        playlists={"later": {"snapshot_id": "abc"}},
        albums=_records(),
        indexer=[{"t": "2026-10-29T10:00:00+00:00", "kind": "hit"},
                 {"t": "2026-10-29T11:00:00+00:00", "kind": "grab"}],
        mixed=[RG],
    )
    with state.connect() as conn:
        assert state.save_albums(conn, rows) == 3
        assert all(r["_id"] for r in rows.albums.values())
        loaded = state.load_albums(conn)
        refs = conn.execute("SELECT ref, kind FROM album_refs ORDER BY ref").fetchall()
        primary = conn.execute("SELECT spotify_album_id FROM albums WHERE release_group = %s", (RG,)).fetchone()
    assert _strip(loaded) == _records()
    assert list(loaded.albums) == list(_records())  # insertion order survives
    assert loaded.playlists == rows.playlists
    assert loaded.indexer == rows.indexer
    assert loaded.mixed == [RG]
    assert refs == [("local/files", "name"), ("sp1", "spotify"), ("sp2", "spotify"), ("sp3", "spotify")]
    assert primary == ("sp1",)
    assert loaded.by_ref() == {"sp1": RG, "sp2": RG, "sp3": "sp3", "local/files": "local/files"}


@needs_pg
def test_save_writes_only_changed_rows_and_leaves_others_alone(pg_url):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records()))
        mine = state.load_albums(conn)
        theirs = state.load_albums(conn)
        theirs.albums["sp9"] = {"status": "wanted", "spotify_album_ids": ["sp9"], "name": "New", "artist": "X"}
        assert state.save_albums(conn, theirs) == 1
        assert state.save_albums(conn, mine) == 0
        mine.albums["sp3"]["status"] = "missing"
        del mine.albums["local/files"]
        assert state.save_albums(conn, mine) == 2
        loaded = state.load_albums(conn)
    assert set(loaded.albums) == {RG, "sp3", "sp9"}
    assert loaded.albums["sp3"]["status"] == "missing"


@needs_pg
def test_resolving_rekeys_in_place_and_clears_the_flag(pg_url):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records()))
        rows = state.load_albums(conn)
        record = rows.albums.pop("sp3")
        row_id = record["_id"]
        record["release_group"] = RG2  # unresolved left set: the save clears it
        rows.albums[RG2] = record
        state.save_albums(conn, rows)
        stored = conn.execute("SELECT id, unresolved FROM albums WHERE release_group = %s", (RG2,)).fetchone()
        loaded = state.load_albums(conn)
    assert stored == (row_id, None)
    assert loaded.albums[RG2]["spotify_album_ids"] == ["sp3"]


@needs_pg
def test_a_merge_moves_a_ref_between_rows(pg_url):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records()))
        rows = state.load_albums(conn)
        gone = rows.albums.pop("sp3")
        rows.albums[RG]["spotify_album_ids"].extend(gone["spotify_album_ids"])
        state.save_albums(conn, rows)
        loaded = state.load_albums(conn)
        count = conn.execute("SELECT count(*) FROM albums").fetchone()
    assert loaded.albums[RG]["spotify_album_ids"] == ["sp1", "sp2", "sp3"]
    assert count == (2,)


@needs_pg
def test_playlist_stores(pg_url):
    with state.connect() as conn:
        assert state.load_store(conn, "later") is None
        stamp = state.save_store(conn, "later", [{"song": {"url": "u1"}}], source="spotify")
        state.save_store(conn, "keep", [])
        assert state.store_names(conn) == ["keep", "later"]
        assert state.load_store(conn, "later") == {
            "playlist": "later", "source": "spotify", "updated": stamp, "entries": [{"song": {"url": "u1"}}]}
        state.remove_store(conn, "keep")
        assert state.store_names(conn) == ["later"]


@needs_pg
def test_failures_are_replaced_whole(pg_url):
    with state.connect() as conn:
        state.save_failures(conn, {"u1": {"attempts": 1}, "u2": {"attempts": 2}})
        state.save_failures(conn, {"u2": {"attempts": 3}})
        assert state.load_failures(conn) == {"u2": {"attempts": 3}}


@needs_pg
def test_kv_documents(pg_url):
    with state.connect() as conn:
        assert state.get(conn, state.SPOTIFY_LIMIT) is None
        state.put(conn, state.SPOTIFY_LIMIT, {"until": "x", "retry_after": 60})
        state.put_many(conn, state.MB_RELEASES, {"sp1": {"rung": "url"}, "sp2": {"rung": "none"}})
        state.put_many(conn, state.MB_RELEASES, {"sp2": {"rung": "isrc"}})
        assert state.get(conn, state.SPOTIFY_LIMIT) == {"until": "x", "retry_after": 60}
        assert state.get_all(conn, state.MB_RELEASES) == {"sp1": {"rung": "url"}, "sp2": {"rung": "isrc"}}
        state.delete(conn, state.MB_RELEASES, ["sp1"])
        assert state.get_all(conn, state.MB_RELEASES) == {"sp2": {"rung": "isrc"}}

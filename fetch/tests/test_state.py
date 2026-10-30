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
        "sp1": {"status": "imported", "release_group": RG, "unresolved": None,
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


def test_album_key_prefers_spotify_then_release_group_then_name():
    assert state.album_key({"release_group": RG, "spotify_album_ids": ["sp", "sq"]}) == "sp"
    assert state.album_key({"release_group": RG, "spotify_album_ids": [], "names": ["a/b"]}) == RG
    assert state.album_key({"spotify_album_ids": [], "names": ["a/b"]}) == "a/b"


def test_is_spotify_album():
    assert state.is_spotify_album("0ETFjACtuP2ADo6LFhL6HN")
    assert not state.is_spotify_album(RG)
    assert not state.is_spotify_album("artist/album")
    assert not state.is_spotify_album("")


def test_json_round_trip_strips_the_database_fields():
    data = {"playlists": {"p": {"snapshot_id": "s"}}, "albums": {"sp1": {"status": "wanted"}, "a/b": {"status": "have"}},
            "indexer": [], "mixed": ["sp1"]}
    rows = state.rows_from_json(data)
    assert rows.albums["sp1"]["spotify_album_ids"] == ["sp1"] and rows.albums["a/b"]["names"] == ["a/b"]
    assert state.rows_to_json(rows) == data
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
        assert _check_values(conn, "albums_origin_check") == {albums.ALBUM, albums.WANTS, albums.LIBRARY, albums.REQUEST}
        assert _check_values(conn, "albums_unresolved_check") == set(state.UNRESOLVED)


@needs_pg
def test_albums_round_trip(pg_url):
    rows = state.AlbumRows(
        playlists={"later": {"snapshot_id": "abc"}},
        albums=_records(),
        indexer=[{"t": "2026-10-29T10:00:00+00:00", "kind": "hit"},
                 {"t": "2026-10-29T11:00:00+00:00", "kind": "grab"}],
        mixed=["sp1"],
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
    assert loaded.mixed == ["sp1"]
    assert refs == [("local/files", "name"), ("sp1", "spotify"), ("sp2", "spotify"), ("sp3", "spotify")]
    assert primary == ("sp1",)
    assert loaded.by_ref() == {"sp1": "sp1", "sp2": "sp1", "sp3": "sp3", "local/files": "local/files"}


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
    assert set(loaded.albums) == {"sp1", "sp3", "sp9"}
    assert loaded.albums["sp3"]["status"] == "missing"


@needs_pg
def test_resolving_keeps_the_row_and_clears_the_flag(pg_url):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records()))
        rows = state.load_albums(conn)
        record = rows.albums["sp3"]
        row_id = record["_id"]
        record["release_group"] = RG2  # unresolved left set: the save clears it
        state.save_albums(conn, rows)
        stored = conn.execute("SELECT id, unresolved FROM albums WHERE release_group = %s", (RG2,)).fetchone()
        loaded = state.load_albums(conn)
    assert stored == (row_id, None)
    assert loaded.albums["sp3"]["release_group"] == RG2


@needs_pg
def test_a_merge_moves_a_ref_between_rows(pg_url):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records()))
        rows = state.load_albums(conn)
        gone = rows.albums.pop("sp3")
        rows.albums["sp1"]["spotify_album_ids"].extend(gone["spotify_album_ids"])
        state.save_albums(conn, rows)
        loaded = state.load_albums(conn)
        count = conn.execute("SELECT count(*) FROM albums").fetchone()
    assert loaded.albums["sp1"]["spotify_album_ids"] == ["sp1", "sp2", "sp3"]
    assert count == (2,)


def test_album_key_by_row_is_the_row_id_once_saved():
    record = {"release_group": RG, "spotify_album_ids": ["sp"]}
    assert state.album_key(record, by_row=True) == "sp"  # not saved yet: the old scheme
    assert state.album_key({**record, "_id": 17}, by_row=True) == "17"
    assert state.album_key({**record, "_id": 17}) == "sp"
    assert state.primary_spotify(record) == "sp" and state.primary_spotify({"names": ["a/b"]}) is None


@needs_pg
def test_keyed_by_row_once_the_marker_is_set(pg_url):
    """homelab#2478: the keys are row ids once music-album-rekey --apply wrote
    the marker; a record not saved yet keeps the old key until its save."""
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records(), mixed=["sp1"]))
        assert not state.keyed_by_row(conn)
        state.put(conn, state.ALBUM_KEYS, {"by": "row"})
        rows = state.load_albums(conn)
        ids = {r["spotify_album_ids"][0] if r["spotify_album_ids"] else r["names"][0]: k
               for k, r in rows.albums.items()}
        rows.albums["sp9"] = state.new_record("sp9", status="wanted", name="New", artist="X")
        rows.mixed.append("sp9")
        assert "sp9" in rows.albums
        state.save_albums(conn, rows)
        new_id = next(k for k, r in rows.albums.items() if r.get("name") == "New")
        reloaded = state.load_albums(conn)
    assert rows.by_row and all(k.isdigit() for k in rows.albums)
    assert ids["sp1"] == str(rows.albums[ids["sp1"]]["_id"])
    assert "sp9" not in rows.albums and new_id in rows.albums and rows.mixed == [ids["sp1"], new_id]
    assert list(reloaded.albums) == list(rows.albums) and reloaded.mixed == rows.mixed
    assert reloaded.by_ref()["sp2"] == ids["sp1"]


@needs_pg
def test_a_deleted_row_leaves_its_refs(pg_url):
    """A merge's loser and an album that left its playlists are deleted; their
    refs stay in album_rows_gone, for the items stamped with their row ids."""
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=_records()))
        rows = state.load_albums(conn)
        sp3, local = rows.albums.pop("sp3"), rows.albums.pop("local/files")
        rows.albums["sp1"]["spotify_album_ids"].extend(sp3["spotify_album_ids"])
        state.save_albums(conn, rows)
        gone = state.gone_rows(conn)
    assert gone == {str(sp3["_id"]): ["sp3"], str(local["_id"]): ["local/files"]}


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


# --- the switch: the JSON files until the import, then the database ---


@pytest.fixture
def switched(tmp_path, monkeypatch, pg_url):
    """The state switched to the test database."""
    switch = tmp_path / ".state-in-db"
    switch.touch()
    monkeypatch.setattr(state, "SWITCH", switch)
    return pg_url


def test_unswitched_needs_no_database(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "SWITCH", tmp_path / ".state-in-db")
    monkeypatch.setenv(db.ENV, "postgresql://nowhere.invalid/music")
    with state.session() as conn:
        assert conn is None
    path = tmp_path / "doc.json"
    state.save_doc(state.EMPTY_SLOTS, path, {"at": "x"})
    assert state.load_doc(state.EMPTY_SLOTS, path) == {"at": "x"}


def test_switched_and_down_is_unavailable(tmp_path, monkeypatch):
    switch = tmp_path / ".state-in-db"
    switch.touch()
    monkeypatch.setattr(state, "SWITCH", switch)
    monkeypatch.delenv(db.ENV, raising=False)
    with pytest.raises(state.Unavailable):
        with state.session():
            pass
    cache = state.KeyedCache(state.MB_RELEASES, tmp_path / "cache.json")
    assert cache.load() == {}  # a cache is best-effort
    cache.save({"sp1": {}})
    assert not (tmp_path / "cache.json").exists()


@needs_pg
def test_switched_playlist_stores(switched, tmp_path, monkeypatch):
    from music_fetch import store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    playlist = store.Store("later", [{"song": {"url": "u1"}}], source=store.SPOTIFY)
    store.save(playlist)
    assert playlist.updated
    assert store.names() == ["later"]
    assert store.load("later").entries == [{"song": {"url": "u1"}}]
    assert store.songs_by_playlist() == {"later": [{"url": "u1"}]}
    assert not (tmp_path / "store").exists()  # nothing written to the JSON side
    store.remove("later")
    assert store.load("later") is None and store.names() == []


@needs_pg
def test_switched_backoff_and_rate_limit(switched, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from music_fetch import spotdl_ops, spotify_limit

    spotdl_ops._save_failures(tmp_path / "failures.json", {"u1": {"attempts": 1}})
    assert spotdl_ops._load_failures(tmp_path / "failures.json") == {"u1": {"attempts": 1}}
    assert not (tmp_path / "failures.json").exists()
    monkeypatch.setattr(spotify_limit, "STATE_FILE", tmp_path / "limit.json")
    monkeypatch.setattr(spotify_limit, "_limit", None)
    spotify_limit.trip(600)
    until, seconds = spotify_limit._load(tmp_path / "limit.json")
    assert seconds == 600 and until > datetime.now(timezone.utc)
    assert not (tmp_path / "limit.json").exists()


@needs_pg
def test_switched_cache_writes_only_changed_keys(switched, tmp_path):
    cache = state.KeyedCache(state.MB_RELEASES, tmp_path / "cache.json")
    data = cache.load()
    data.update(sp1={"rung": "url"}, sp2={"rung": "none"})
    cache.save(data)
    with state.connect() as conn:
        conn.execute("UPDATE kv SET value = '{\"rung\": \"isrc\"}' WHERE key = 'sp1'")  # another process
        again = state.KeyedCache(state.MB_RELEASES, tmp_path / "cache.json")
        loaded = again.load()
        loaded["sp2"] = {"rung": "upc"}
        again.save(loaded)
        assert state.get_all(conn, state.MB_RELEASES) == {"sp1": {"rung": "isrc"}, "sp2": {"rung": "upc"}}
    cache.save({**data, "sp3": {"rung": "url"}})  # sp1 unchanged here, so the other process's write stays
    with state.connect() as conn:
        assert state.get(conn, state.MB_RELEASES, "sp1") == {"rung": "isrc"}
    assert not (tmp_path / "cache.json").exists()


@needs_pg
def test_switched_documents(switched, tmp_path):
    state.save_doc(state.CANON_PAGES, tmp_path / "pages.json", {"read_at": 1.0, "playlists": {}})
    assert state.load_doc(state.CANON_PAGES, tmp_path / "pages.json") == {"read_at": 1.0, "playlists": {}}
    assert state.load_doc(state.EMPTY_SLOTS, tmp_path / "slots.json") is None
    assert not (tmp_path / "pages.json").exists()

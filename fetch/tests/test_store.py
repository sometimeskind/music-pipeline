"""Tests for music_fetch.store, the playlist store (homelab#1914)."""

from datetime import datetime, timezone

from music_fetch import store

URL = "https://open.spotify.com/track/"
LOCAL = {"local": {"id": 7, "path": "/library/local.m4a", "title": "Local", "artist": "Artist"}}


def song(sid: str, **extra) -> dict:
    return {"url": URL + sid, "song_id": sid, "name": f"Song {sid}", "artists": ["Artist"], **extra}


def test_store_round_trip(tmp_path) -> None:
    s = store.Store.from_songs("pl", [song("A"), song("B")])
    s.save(tmp_path / "pl.json")
    loaded = store.Store.load(tmp_path / "pl.json")
    assert loaded is not None
    assert loaded.name == "pl" and loaded.songs == [song("A"), song("B")]
    assert loaded.updated
    assert not (tmp_path / "pl.json.tmp").exists()


def test_store_load_missing_or_broken(tmp_path) -> None:
    assert store.Store.load(tmp_path / "none.json") is None
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert store.Store.load(tmp_path / "bad.json") is None


def test_merge_spotify_takes_order_and_membership_from_spotify() -> None:
    full_b = song("B", album_name="Full record")
    merged = store.merge_spotify([song("C"), song("B"), song("C")], {URL + "B": full_b, URL + "A": song("A")})
    # A left Spotify and is gone; B keeps its richer record; the duplicate C stays.
    assert merged == [song("C"), full_b, song("C")]


def test_want_list() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    failures = {
        URL + "D": {"retry_after": "2026-10-20T00:00:00+00:00"},  # still backed off
        URL + "E": {"retry_after": "2026-10-01T00:00:00+00:00"},  # backoff over
    }
    songs = [song(x) for x in "ABCDEFA"]
    wanted = store.want_list(songs, resolves=lambda s: s["song_id"] == "B", held={URL + "C"}, failures=failures,
                             now=now)
    assert wanted == [URL + "A", URL + "E", URL + "F"]


def test_names_lists_the_stores_sorted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    assert store.names() == []
    for name in ("later", "aaaaaaah", "wedding"):
        store.Store.from_songs(name, [song("A")]).save(store.path(name))
    (tmp_path / "store" / "later.json.tmp").write_text("{", encoding="utf-8")  # a crashed write
    assert store.names() == ["aaaaaaah", "later", "wedding"]


def test_remove_deletes_one_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    store.Store.from_songs("gone", [song("A")]).save(store.path("gone"))
    store.Store.from_songs("kept", [song("B")]).save(store.path("kept"))
    store.remove("gone")
    store.remove("never-had-one")  # missing is fine
    assert store.names() == ["kept"]
    assert store.load("gone") is None


def test_songs_by_playlist_skips_local_entries_and_broken_stores(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    store.Store("pl", [{"song": song("A")}, LOCAL, {"song": song("B")}]).save(store.path("pl"))
    store.Store.from_songs("empty", []).save(store.path("empty"))
    (tmp_path / "store" / "bad.json").write_text("{", encoding="utf-8")
    assert store.songs_by_playlist() == {"empty": [], "pl": [song("A"), song("B")]}


# ---------------------------------------------------------------------------
# Local entries (homelab#1914 stage b)
# ---------------------------------------------------------------------------


def test_from_songs_keeps_an_earlier_stores_local_entries() -> None:
    earlier = store.Store("pl", [{"song": song("A")}, LOCAL])
    rebuilt = store.Store.from_songs("pl", [song("B")], keep=earlier)
    assert rebuilt.entries == [{"song": song("B")}, LOCAL]
    assert rebuilt.songs == [song("B")] and rebuilt.locals == [LOCAL["local"]]

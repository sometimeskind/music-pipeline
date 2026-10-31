"""Tests for music_fetch.store, the playlist store (homelab#1914)."""

from datetime import datetime, timezone

from music_fetch import store

URL = "https://open.spotify.com/track/"
LOCAL = {"local": {"id": 7, "path": "/library/local.m4a", "title": "Local", "artist": "Artist"}}


def song(sid: str, **extra) -> dict:
    return {"url": URL + sid, "song_id": sid, "name": f"Song {sid}", "artists": ["Artist"], **extra}


def test_store_round_trip() -> None:
    s = store.Store.from_songs("pl", [song("A"), song("B")])
    store.save(s)
    loaded = store.load("pl")
    assert loaded is not None
    assert loaded.name == "pl" and loaded.songs == [song("A"), song("B")]
    assert loaded.updated and loaded.updated == s.updated


def test_store_load_missing() -> None:
    assert store.load("none") is None


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


def test_names_lists_the_stores_sorted() -> None:
    assert store.names() == []
    for name in ("later", "aaaaaaah", "wedding"):
        store.save(store.Store.from_songs(name, [song("A")]))
    assert store.names() == ["aaaaaaah", "later", "wedding"]


def test_remove_deletes_one_store() -> None:
    store.save(store.Store.from_songs("gone", [song("A")]))
    store.save(store.Store.from_songs("kept", [song("B")]))
    store.remove("gone")
    store.remove("never-had-one")  # missing is fine
    assert store.names() == ["kept"]
    assert store.load("gone") is None


def test_songs_by_playlist_skips_local_entries() -> None:
    store.save(store.Store("pl", [{"song": song("A")}, LOCAL, {"song": song("B")}]))
    store.save(store.Store.from_songs("empty", []))
    assert store.songs_by_playlist() == {"empty": [], "pl": [song("A"), song("B")]}


# ---------------------------------------------------------------------------
# Local entries (homelab#1914 stage b)
# ---------------------------------------------------------------------------


def test_from_songs_keeps_an_earlier_stores_local_entries() -> None:
    earlier = store.Store("pl", [{"song": song("A")}, LOCAL])
    rebuilt = store.Store.from_songs("pl", [song("B")], keep=earlier)
    assert rebuilt.entries == [{"song": song("B")}, LOCAL]
    assert rebuilt.songs == [song("B")] and rebuilt.locals == [LOCAL["local"]]

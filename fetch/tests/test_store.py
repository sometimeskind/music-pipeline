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


# --- membership events (homelab#2361) ---


def _events() -> list[tuple]:
    from music_fetch import state  # noqa: PLC0415

    with state.session() as conn:
        return conn.execute("SELECT kind, source, item_id, message, data FROM events ORDER BY id").fetchall()


def test_changes_counts_duplicates_and_ignores_order() -> None:
    before = [{"song": song("A")}, {"song": song("B")}, {"song": song("B")}, LOCAL]
    after = [{"song": song("B")}, {"song": song("C")}, {"song": song("A")}, LOCAL]
    added, removed = store.changes(before, after)
    assert [store.entry_key(e) for e in added] == ["spotify:C"]
    assert [store.entry_key(e) for e in removed] == ["spotify:B"]
    assert store.changes(after, list(reversed(after))) == ([], [])


def test_entry_key_falls_back_to_the_url_and_keys_locals_by_item() -> None:
    assert store.entry_key({"song": {"url": URL + "X"}}) == "spotify:X"
    assert store.entry_key({"song": {"url": "https://musicbrainz.org/recording/r"}}) == \
        "url:https://musicbrainz.org/recording/r"
    assert store.entry_key(LOCAL) == "local:7"


def test_a_first_save_records_one_summary_event() -> None:
    store.save(store.Store.from_songs("pl", [song("A"), song("B")]))
    [(kind, source, item_id, message, data)] = _events()
    assert (kind, source, item_id) == ("playlist_entry_added", "store", None)
    assert message == "[STORE] pl: +2 entries (spotify, created)"
    assert data["count"] == 2 and data["created"] is True
    assert [e["song_id"] for e in data["entries"]] == ["A", "B"]


def test_a_save_records_each_entry_added_and_removed() -> None:
    store.save(store.Store.from_songs("pl", [song("A"), song("B")]))
    store.save(store.Store.from_songs("pl", [song("B", isrc="USX1"), song("C", isrc="USX2"), song("A")]))
    store.save(store.Store.from_songs("pl", [song("C")]))
    rows = _events()[1:]
    assert [(k, m) for k, _, _, m, _ in rows] == [
        ("playlist_entry_added", "[STORE] pl: + Artist — Song C (spotify)"),
        ("playlist_entry_removed", "[STORE] pl: - Artist — Song B (spotify)"),
        ("playlist_entry_removed", "[STORE] pl: - Artist — Song A (spotify)"),
    ]
    assert rows[0][4] == {"playlist": "pl", "by": "spotify", "key": "spotify:C", "song_id": "C", "isrc": "USX2",
                          "url": URL + "C", "artist": "Artist", "title": "Song C"}


def test_a_reorder_or_a_richer_record_is_no_event() -> None:
    store.save(store.Store.from_songs("pl", [song("A"), song("B")]))
    store.save(store.Store.from_songs("pl", [song("B", album_name="Full"), song("A")]))
    assert len(_events()) == 1


def test_a_local_entry_dropped_by_regen_names_its_item_and_who() -> None:
    store.save(store.Store("pl", [{"song": song("A")}, LOCAL]))
    store.save(store.Store("pl", [{"song": song("A")}]), by="regen")
    kind, _, item_id, message, data = _events()[-1]
    assert (kind, item_id, message) == ("playlist_entry_removed", 7, "[STORE] pl: - Artist — Local (regen)")
    assert data["by"] == "regen" and data["path"] == "/library/local.m4a"


def test_a_bulk_change_records_one_event_per_kind(monkeypatch) -> None:
    monkeypatch.setattr(store, "EVENTS_PER_ENTRY", 3)
    store.save(store.Store.from_songs("pl", [song(c) for c in "ABCD"]))
    store.save(store.Store.from_songs("pl", [song("E")]))
    rows = _events()[1:]
    assert [(k, m) for k, _, _, m, _ in rows] == [
        ("playlist_entry_added", "[STORE] pl: +1 entries (spotify, bulk)"),
        ("playlist_entry_removed", "[STORE] pl: -4 entries (spotify, bulk)"),
    ]
    assert rows[1][4]["count"] == 4 and rows[1][4]["bulk"] is True


def test_an_unchanged_save_records_nothing() -> None:
    s = store.Store.from_songs("pl", [song("A")])
    store.save(s)
    store.save(s)
    assert len(_events()) == 1

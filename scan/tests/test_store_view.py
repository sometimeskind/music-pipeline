"""The playlist store at regen (homelab#1914 stage c): the store is the only
playlist state.  Each ``.m3u`` is the store's view (entries resolved by
Spotify ID or ISRC against the whole library), then its existing local
entries.  ``.spotdl`` files are never read, and the beets ``sources`` field is
provenance only: an item tagged with a playlist that no entry lists is not on it."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest import mock

import pytest

from music_fetch import store


def _item(title, path, **flex):
    it = mock.MagicMock(title=title, artist="Artist", albumartist="Artist")
    it.path = str(path).encode()
    it.get.side_effect = lambda k, d=None: flex.get(k, d)
    return it


def _song(name, song_id):
    return {"name": name, "artists": ["Artist"], "url": f"https://open.spotify.com/track/{song_id}", "song_id": song_id}


def _local(item_id, title):
    return {store.LOCAL: {"id": item_id, "path": "x", "title": title, "artist": "Artist"}}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A library of four items, ``pl`` among the playlists, and no store yet.

    ``extra`` is tagged ``sources=pl`` and ``words`` matches an entry by
    title+artist only: neither is on the playlist by its store."""
    lib_root = tmp_path / "library"
    tagged = _item("Tagged", lib_root / "tagged.m4a", spotify_ids="T1", sources="pl")
    words = _item("Words", lib_root / "words.m4a", sources="pl")
    extra = _item("Extra", lib_root / "extra.m4a", sources="pl")
    elsewhere = _item("Elsewhere", lib_root / "elsewhere.m4a", spotify_ids="E1", sources="other")
    for n, it in enumerate((tagged, words, extra, elsewhere), 1):
        it.id = n
    spotdl_dir, playlists_dir = tmp_path / "spotdl", tmp_path / "playlists"
    spotdl_dir.mkdir()
    playlists_dir.mkdir()
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.all_items.return_value = [tagged, words, extra, elsewhere]
    lib.items_by_source.side_effect = AssertionError("regen never reads sources")
    monkeypatch.setattr("music_scan.scan.SPOTDL_DIR", spotdl_dir)
    monkeypatch.setattr("music_scan.scan.PLAYLISTS", playlists_dir)
    monkeypatch.setattr("music_scan.scan.MusicLibrary", mock.MagicMock(return_value=lib))
    return spotdl_dir, playlists_dir, lib


def _whole_list() -> None:
    """The store the nightly wrote: the whole Spotify list."""
    store.save(store.Store.from_songs("pl", [_song("Tagged", "T1"), _song("Words", "W1"), _song("Elsewhere", "E1"),
                                  _song("Nowhere", "N1")]))


def _m3u(playlists_dir: Path, name: str = "pl") -> list[str]:
    return (playlists_dir / f"{name}.m3u").read_text(encoding="utf-8").splitlines()


def test_the_m3u_is_the_store_view(env, caplog) -> None:
    """Store order, by ID/ISRC only: Words (a title+artist match) and Extra
    (tagged ``sources=pl``, listed by no entry) are not on it."""
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir, _ = env
    # A stale .spotdl is left on disk and never read.
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [_song("Extra", "X1")]}), encoding="utf-8")
    _whole_list()
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        counts = regen_playlists()

    assert _m3u(playlists_dir) == ["../library/tagged.m4a", "../library/elsewhere.m4a"]
    assert counts == {"pl": 2}
    assert "[STORE-TAIL] pl: 0 local entries, 0 resolved" in caplog.text


def test_a_tagged_item_no_entry_lists_is_not_on_the_playlist(env) -> None:
    """The stage c regression: ``sources`` is provenance only.  An item tagged
    with the playlist that no store entry lists is not in the ``.m3u``, and no
    local entry is created for it, however many scans run."""
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    _whole_list()
    before = store.load("pl").entries
    for _ in range(2):
        regen_playlists()
        lines = _m3u(playlists_dir)
        assert "../library/extra.m4a" not in lines
        assert "../library/words.m4a" not in lines
        saved = store.load("pl")
        assert saved.locals == []
        assert saved.entries == before


def test_existing_local_entries_follow_the_view_in_store_order(env) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    store.save(store.Store("pl", [{"song": _song("Tagged", "T1")}, _local(3, "Extra"), _local(2, "Words")]
                ))
    counts = regen_playlists()
    assert _m3u(playlists_dir) == ["../library/tagged.m4a", "../library/extra.m4a", "../library/words.m4a"]
    assert counts == {"pl": 3}
    # Read, not rewritten: both stay.
    assert [(e["id"], e["title"]) for e in store.load("pl").locals] == [(3, "Extra"), (2, "Words")]


def test_a_local_entry_stays_whatever_its_items_tags(env) -> None:
    """Elsewhere is tagged ``sources=other`` only: its local entry keeps it on ``pl``."""
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    store.save(store.Store("pl", [{"song": _song("Tagged", "T1")}, _local(4, "Elsewhere")]))
    regen_playlists()
    assert _m3u(playlists_dir) == ["../library/tagged.m4a", "../library/elsewhere.m4a"]


def test_a_local_entry_whose_item_is_gone_is_left_out(env) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    store.save(store.Store("pl", [{"song": _song("Tagged", "T1")}, _local(99, "Gone")]))
    regen_playlists()
    assert _m3u(playlists_dir) == ["../library/tagged.m4a"]
    # Not resolved by the view, so the entry stays in the store.
    assert [e["id"] for e in store.load("pl").locals] == [99]


def test_a_local_entry_the_store_now_resolves_is_dropped(env, caplog) -> None:
    """Extra gained Spotify ID X1, which the playlist lists: its Spotify entry
    now decides its place, and whether it stays."""
    from music_scan.scan import regen_playlists

    _, playlists_dir, lib = env
    store.save(store.Store("pl", [{"song": _song("Extra", "X1")}, {"song": _song("Tagged", "T1")},
                       _local(2, "Words"), _local(3, "Extra")]))
    extra = lib.all_items.return_value[2]
    extra.get.side_effect = lambda k, d=None: {"spotify_ids": "X1", "sources": "pl"}.get(k, d)
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        regen_playlists()

    saved = store.load("pl")
    assert [e["title"] for e in saved.locals] == ["Words"]
    assert [s["song_id"] for s in saved.songs] == ["X1", "T1"]
    assert "[STORE-TAIL] pl: 1 local entry, 1 resolved" in caplog.text
    assert _m3u(playlists_dir) == ["../library/extra.m4a", "../library/tagged.m4a", "../library/words.m4a"]


def test_a_playlist_with_no_store_is_skipped(env, caplog) -> None:
    """Only playlists with a store are regenerated: a .spotdl alone is not one,
    and its .m3u is left as it was."""
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir, _ = env
    (spotdl_dir / "old.spotdl").write_text(json.dumps({"songs": [_song("Tagged", "T1")]}), encoding="utf-8")
    (playlists_dir / "old.m3u").write_text("kept\n", encoding="utf-8")
    _whole_list()
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        counts = regen_playlists()

    assert counts == {"pl": 2}
    assert (playlists_dir / "old.m3u").read_text(encoding="utf-8") == "kept\n"


def test_no_stores_writes_nothing(env) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir, _ = env
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [_song("Tagged", "T1")]}), encoding="utf-8")
    assert regen_playlists() == {}
    assert list(playlists_dir.iterdir()) == []


def test_a_failing_store_leaves_the_old_m3u(env, caplog) -> None:
    """A store failure never fails a scan or empties a playlist; the others still regenerate."""
    from music_scan import scan

    _, playlists_dir, _ = env
    _whole_list()
    store.save(store.Store.from_songs("other", [_song("Elsewhere", "E1")]))
    (playlists_dir / "pl.m3u").write_text("../library/old.m4a\n", encoding="utf-8")
    real = scan.store_view

    def view(name, *args):
        if name == "pl":
            raise RuntimeError("boom")
        return real(name, *args)

    with mock.patch("music_scan.scan.store_view", side_effect=view):
        counts = scan.regen_playlists()
    assert counts == {"other": 1}
    assert _m3u(playlists_dir) == ["../library/old.m4a"]
    assert _m3u(playlists_dir, "other") == ["../library/elsewhere.m4a"]
    assert "Playlist store failed for pl" in caplog.text


def test_a_failing_local_tail_leaves_the_old_m3u(env, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    _whole_list()
    (playlists_dir / "pl.m3u").write_text("../library/old.m4a\n", encoding="utf-8")
    with mock.patch("music_scan.scan._local_tail", side_effect=RuntimeError("boom")):
        assert regen_playlists() == {}
    assert _m3u(playlists_dir) == ["../library/old.m4a"]
    assert "Playlist store failed for pl" in caplog.text


def test_an_album_playlist_is_its_stores_album_view(env) -> None:
    """An ``album`` playlist lists :func:`playlists.album_paths` over its store's
    songs, resolved with an empty source index, then its local entries."""
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir, _ = env
    (spotdl_dir / "pl.album").touch()
    store.save(store.Store("pl", [{"song": _song("Elsewhere", "E1")}, _local(3, "Extra")]))
    album = playlists_dir.parent / "library" / "album" / "01.m4a"
    with mock.patch("music_scan.playlists.album_paths", return_value=([album], [], 1)) as album_paths:
        counts = regen_playlists()
    songs, source, library, _, _ = album_paths.call_args.args
    assert [s["song_id"] for s in songs] == ["E1"]
    assert source.items == []
    assert len(library.items) == 4
    assert _m3u(playlists_dir) == ["../library/album/01.m4a", "../library/extra.m4a"]
    assert counts == {"pl": 2}


def test_regen_records_the_empty_slots(env) -> None:
    from music_scan.metrics import ScanMetrics
    from music_scan.scan import regen_playlists

    _whole_list()
    store.save(store.Store.from_songs("full", [_song("Tagged", "T1")]))
    metrics = ScanMetrics()
    regen_playlists(metrics)
    # pl: W1 (a words-only match) and N1 are empty.
    assert metrics.slots_empty == {"full": 0, "pl": 2}
    from music_fetch import state

    with state.session() as conn:
        report = state.get(conn, state.EMPTY_SLOTS)
    assert report["playlists"]["full"] == {"total": 0, "entries": []}
    assert report["playlists"]["pl"]["total"] == 2 and len(report["playlists"]["pl"]["entries"]) == 2


# ---------------------------------------------------------------------------
# store_listings: the listings top-ups ask
# ---------------------------------------------------------------------------


def test_store_listings_by_recording(env) -> None:
    from music_scan.scan import store_listings

    store.save(store.Store("pl", [{"song": _song("Tagged", "T1")}, {"song": {**_song("Isrc", "I1"), "isrc": "GB1"}},
                       {"song": {"name": "Url only", "artists": ["Artist"],
                                 "url": "https://open.spotify.com/track/U1"}},
                       _local(9, "Local")]))
    store.save(store.Store.from_songs("album", [_song("Tagged", "T1")]))
    listed = store_listings()
    # A loose copy of T1 is listed by both stores, whichever item the index holds for T1.
    assert listed(_item("Copy", "/c.m4a", spotify_ids="T1")) == {"pl", "album"}
    assert listed(_item("By ISRC", "/i.m4a", isrc="GB1")) == {"pl"}
    assert listed(_item("By URL", "/u.m4a", spotify_ids="U1")) == {"pl"}
    local = _item("Local", "/l.m4a")
    local.id = 9
    assert listed(local) == {"pl"}
    # Tagged with the playlist, listed by no entry: not listed.
    assert listed(_item("Nowhere", "/n.m4a", spotify_ids="Z9", sources="pl")) == set()


def test_store_listings_without_stores_lists_nothing(env) -> None:
    from music_scan.scan import store_listings

    listed = store_listings()
    assert callable(listed)
    assert listed(_item("Tagged", "/t.m4a", spotify_ids="T1", sources="pl")) == set()

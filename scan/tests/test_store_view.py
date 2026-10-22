"""The playlist store at regen (homelab#1914): in the dry run the store's
``.m3u`` goes to the side directory, diffed against today's, and nothing else
changes; with ``on`` it is the playlist, followed by its local entries, which
the ``sources`` tail seeds (stage b)."""

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


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A library of four items and a playlist ``pl`` whose .spotdl holds the
    downloaded entries only, as a spotdl playlist's does."""
    lib_root = tmp_path / "library"
    tagged = _item("Tagged", lib_root / "tagged.m4a", spotify_ids="T1", sources="pl")
    words = _item("Words", lib_root / "words.m4a", sources="pl")  # matched by title+artist only
    extra = _item("Extra", lib_root / "extra.m4a", sources="pl")  # tagged, no entry: the tail
    elsewhere = _item("Elsewhere", lib_root / "elsewhere.m4a", spotify_ids="E1", sources="other")
    for n, it in enumerate((tagged, words, extra, elsewhere), 1):
        it.id = n
    spotdl_dir, playlists_dir, side = tmp_path / "spotdl", tmp_path / "playlists", tmp_path / "side"
    spotdl_dir.mkdir()
    playlists_dir.mkdir()
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [_song("Tagged", "T1"), _song("Words", "W1")]}),
                                          encoding="utf-8")
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    sources = {"pl": [tagged, words, extra], "other": [elsewhere]}
    lib.items_by_source.side_effect = lambda name: sources.get(name, [])
    lib.all_items.return_value = [tagged, words, extra, elsewhere]
    monkeypatch.setattr("music_scan.scan.SPOTDL_DIR", spotdl_dir)
    monkeypatch.setattr("music_scan.scan.PLAYLISTS", playlists_dir)
    monkeypatch.setattr("music_scan.scan.MusicLibrary", mock.MagicMock(return_value=lib))
    monkeypatch.setattr(store, "DRYRUN_DIR", side)
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    return spotdl_dir, playlists_dir, side


def test_off_writes_no_side_directory(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, side = env
    monkeypatch.delenv(store.STORE_ENV, raising=False)
    regen_playlists()
    assert (playlists_dir / "pl.m3u").exists()
    assert not side.exists()


def test_dry_run_writes_the_store_m3u_and_its_diff(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, side = env
    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    # The nightly's dry run wrote the whole Spotify list.
    store.Store.from_songs("pl", [_song("Tagged", "T1"), _song("Words", "W1"), _song("Elsewhere", "E1"),
                                  _song("Nowhere", "N1")]).save(side / "pl.json")
    with caplog.at_level(logging.INFO, logger="music_fetch.store"):
        regen_playlists()

    today = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert today == ["../library/tagged.m4a", "../library/words.m4a", "../library/extra.m4a"]
    # The store holds every entry and resolves by ID/ISRC only, with no sources tail.
    assert (side / "pl.m3u").read_text(encoding="utf-8").splitlines() == ["../library/tagged.m4a",
                                                                          "../library/elsewhere.m4a"]
    assert (side / "pl.m3u.diff").read_text(encoding="utf-8").splitlines() == [
        "+ ../library/elsewhere.m4a", "- ../library/words.m4a", "- ../library/extra.m4a"]
    assert "[STORE-DIFF] pl m3u: today 3 | store 2 | +1 -2" in caplog.text


def test_dry_run_without_a_store_skips_a_spotdl_playlist(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, _, side = env
    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        regen_playlists()
    assert "[STORE] pl: no store yet" in caplog.text
    assert not (side / "pl.m3u").exists()


def test_dry_run_seeds_a_nosync_store_from_its_spotdl(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir, _, side = env
    (spotdl_dir / "pl.nosync").touch()
    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    regen_playlists()
    assert [s["song_id"] for s in store.Store.load(side / "pl.json").songs] == ["T1", "W1"]
    assert (side / "pl.m3u").read_text(encoding="utf-8").splitlines() == ["../library/tagged.m4a"]


def test_dry_run_keeps_a_nosync_store_built_from_spotify(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir, _, side = env
    (spotdl_dir / "pl.nosync").touch()
    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    # The nightly built it from Spotify: E1 is on the playlist but not in the .spotdl.
    built = store.Store.from_songs("pl", [_song("Tagged", "T1"), _song("Elsewhere", "E1")])
    built.source = store.SPOTIFY
    built.save(side / "pl.json")
    regen_playlists()
    assert [s["song_id"] for s in store.Store.load(side / "pl.json").songs] == ["T1", "E1"]
    assert (side / "pl.m3u").read_text(encoding="utf-8").splitlines() == ["../library/tagged.m4a",
                                                                          "../library/elsewhere.m4a"]


def test_a_failing_dry_run_leaves_the_playlist_alone(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    with mock.patch("music_scan.scan.store_view", side_effect=RuntimeError("boom")):
        counts = regen_playlists()
    assert counts == {"pl": 3}
    assert (playlists_dir / "pl.m3u").exists()
    assert "Playlist store failed for pl" in caplog.text


# ---------------------------------------------------------------------------
# PLAYLIST_STORE=on (homelab#1914 stage a)
# ---------------------------------------------------------------------------


def _whole_list(directory: Path) -> None:
    """The store the nightly wrote: the whole Spotify list."""
    store.Store.from_songs("pl", [_song("Tagged", "T1"), _song("Words", "W1"), _song("Elsewhere", "E1"),
                                  _song("Nowhere", "N1")]).save(directory / "pl.json")


def test_on_writes_the_store_view_then_the_sources_tail(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    _whole_list(store.STORE_DIR)
    with caplog.at_level(logging.INFO, logger="music_fetch.store"):
        counts = regen_playlists()

    # Store order by ID/ISRC, then the tagged items no entry resolves (Words is a [WORDS]-only
    # match today), alphabetically.
    expected = ["../library/tagged.m4a", "../library/elsewhere.m4a", "../library/extra.m4a",
                "../library/words.m4a"]
    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines() == expected
    assert counts == {"pl": 4}
    # The diff against what today's path would have written still runs, in the store directory.
    assert (store.STORE_DIR / "pl.m3u.diff").read_text(encoding="utf-8").splitlines() == [
        "+ ../library/elsewhere.m4a", "~ the lines both hold are in another order"]
    assert "[STORE-DIFF] pl m3u: today 3 | store 4 | +1 -0 | order differs" in caplog.text


def test_on_reads_a_store_the_dry_run_built(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, side = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    _whole_list(side)
    regen_playlists()
    assert "../library/elsewhere.m4a" in (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()


def test_on_without_a_store_writes_todays_playlist(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    regen_playlists()
    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines() == [
        "../library/tagged.m4a", "../library/words.m4a", "../library/extra.m4a"]


def test_on_a_failing_store_writes_todays_playlist(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    _whole_list(store.STORE_DIR)
    with mock.patch("music_scan.scan.store_view", side_effect=RuntimeError("boom")):
        counts = regen_playlists()
    assert counts == {"pl": 3}
    assert "Playlist store failed for pl" in caplog.text


def test_on_leaves_an_album_playlist_on_todays_path(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir, _ = env
    (spotdl_dir / "pl.album").touch()
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    with mock.patch("music_scan.playlists.album_paths",
                    side_effect=[([Path("/x/today.m4a")], [], 1), ([Path("/x/store.m4a")], [], 1)]):
        regen_playlists()
    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert [Path(line).name for line in lines] == ["today.m4a"]
    assert (store.STORE_DIR / "pl.m3u").exists()


# ---------------------------------------------------------------------------
# Stage b: local entries, album stores, the listings top-ups ask
# ---------------------------------------------------------------------------


def test_on_turns_the_sources_tail_into_local_entries(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    _whole_list(store.STORE_DIR)
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        regen_playlists()
    saved = store.Store.load(store.STORE_DIR / "pl.json")
    assert [(e["id"], e["title"]) for e in saved.locals] == [(3, "Extra"), (2, "Words")]
    assert [e["song_id"] for e in saved.songs] == ["T1", "W1", "E1", "N1"]
    assert "[STORE-TAIL] pl: 2 local entries, 2 new, 0 resolved" in caplog.text

    # Seeded once: the next regen reads them and adds nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        regen_playlists()
    assert "[STORE-TAIL] pl: 2 local entries, 0 new, 0 resolved" in caplog.text
    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines() == [
        "../library/tagged.m4a", "../library/elsewhere.m4a", "../library/extra.m4a", "../library/words.m4a"]


def test_a_local_entry_stays_when_its_item_loses_the_tag(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    store.Store("pl", [{"song": _song("Tagged", "T1")},
                       {store.LOCAL: {"id": 4, "path": "x", "title": "Elsewhere", "artist": "Artist"}}]
                ).save(store.STORE_DIR / "pl.json")
    regen_playlists()
    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["../library/tagged.m4a", "../library/elsewhere.m4a"]


def test_a_local_entry_whose_item_is_gone_is_left_out(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    store.Store("pl", [{"song": _song("Tagged", "T1")},
                       {store.LOCAL: {"id": 99, "path": "x", "title": "Gone", "artist": "Artist"}}]
                ).save(store.STORE_DIR / "pl.json")
    regen_playlists()
    # Tagged from the view, then the sources tail; the gone item's entry adds no line.
    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines() == [
        "../library/tagged.m4a", "../library/extra.m4a", "../library/words.m4a"]


def test_an_album_store_the_tick_wrote_is_kept(env, monkeypatch) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir, _, _ = env
    (spotdl_dir / "pl.album").touch()
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    ticked = store.Store.from_songs("pl", [_song("Elsewhere", "E1")])
    ticked.source = store.SPOTIFY
    ticked.save(store.STORE_DIR / "pl.json")
    with mock.patch("music_scan.playlists.album_paths", return_value=([], [], 0)):
        regen_playlists()
    assert [s["song_id"] for s in store.Store.load(store.STORE_DIR / "pl.json").songs] == ["E1"]


def test_store_listings_by_recording(env, monkeypatch) -> None:
    from music_scan.scan import store_listings

    spotdl_dir, _, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    (spotdl_dir / "album.spotdl").write_text("{}", encoding="utf-8")
    store.Store("pl", [{"song": _song("Tagged", "T1")}, {"song": {**_song("Isrc", "I1"), "isrc": "GB1"}},
                       {store.LOCAL: {"id": 9, "path": "x", "title": "Local", "artist": "Artist"}}]
                ).save(store.STORE_DIR / "pl.json")
    store.Store.from_songs("album", [_song("Tagged", "T1")]).save(store.STORE_DIR / "album.json")
    listed = store_listings()
    # A loose copy of T1 is listed by both stores, whichever item the index holds for T1.
    assert listed(_item("Copy", "/c.m4a", spotify_ids="T1")) == {"pl", "album"}
    assert listed(_item("By ISRC", "/i.m4a", isrc="GB1")) == {"pl"}
    local = _item("Local", "/l.m4a")
    local.id = 9
    assert listed(local) == {"pl"}
    assert listed(_item("Nowhere", "/n.m4a", spotify_ids="Z9")) == set()


def test_store_listings_fall_back_to_sources(env, monkeypatch) -> None:
    from music_scan.scan import store_listings

    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    assert store_listings() is None
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    assert store_listings() is None  # pl has no store yet


def test_a_local_entry_the_store_now_resolves_is_dropped(env, monkeypatch, caplog) -> None:
    """Extra gained Spotify ID X1, which the playlist lists: its Spotify entry
    now decides its place, and whether it stays."""
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.ON)
    store.Store("pl", [{"song": _song("Tagged", "T1")}, {"song": _song("Words", "W1")},
                       {store.LOCAL: {"id": 3, "path": "x", "title": "Extra", "artist": "Artist"}}]
                ).save(store.STORE_DIR / "pl.json")
    extra = store.Store.load(store.STORE_DIR / "pl.json")
    extra.entries.insert(0, {"song": _song("Extra", "X1")})
    extra.save(store.STORE_DIR / "pl.json")
    from music_scan import scan
    lib = scan.MusicLibrary()
    lib.all_items.return_value[2].get.side_effect = lambda k, d=None: {"spotify_ids": "X1", "sources": "pl"}.get(k, d)
    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        regen_playlists()
    saved = store.Store.load(store.STORE_DIR / "pl.json")
    assert [e["title"] for e in saved.locals] == ["Words"]
    assert "1 resolved" in caplog.text
    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()[:2] == [
        "../library/extra.m4a", "../library/tagged.m4a"]

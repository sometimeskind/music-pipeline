"""The playlist store's dry run at regen (homelab#1914 step 1): the store's
``.m3u`` goes to the side directory, diffed against today's, and nothing else changes."""

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


def test_a_failing_dry_run_leaves_the_playlist_alone(env, monkeypatch, caplog) -> None:
    from music_scan.scan import regen_playlists

    _, playlists_dir, _ = env
    monkeypatch.setenv(store.STORE_ENV, store.DRY_RUN)
    with mock.patch("music_scan.scan.store_view", side_effect=RuntimeError("boom")):
        counts = regen_playlists()
    assert counts == {"pl": 3}
    assert (playlists_dir / "pl.m3u").exists()
    assert "Playlist store dry run failed for pl" in caplog.text

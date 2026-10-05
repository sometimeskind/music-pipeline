"""Tests for music_scan.reconcile — pure-Python logic (no beets process, no network)."""

from __future__ import annotations

import json
import logging
import unittest.mock as mock
from pathlib import Path

from music_scan.reconcile import reconcile_all, reconcile_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_snapshot(path: Path, playlist_name: str, urls: list[str]) -> Path:
    spotdl_file = path / f"{playlist_name}.spotdl"
    songs = [{"url": url, "name": f"Track {i}", "artists": ["Artist"]} for i, url in enumerate(urls)]
    spotdl_file.write_text(
        json.dumps({"type": "sync", "query": ["https://open.spotify.com/playlist/x"], "songs": songs}),
        encoding="utf-8",
    )
    return spotdl_file


def _mock_library(spotify_urls: frozenset[str]) -> mock.Mock:
    lib = mock.Mock()
    lib.spotify_urls_by_source.return_value = spotify_urls
    return lib


# ---------------------------------------------------------------------------
# reconcile_snapshot — all URLs present in library
# ---------------------------------------------------------------------------


def test_reconcile_all_present_no_changes(tmp_path: Path) -> None:
    """When every snapshot URL has a matching entry in the library DB, nothing is dropped."""
    urls = [
        "https://open.spotify.com/track/A",
        "https://open.spotify.com/track/B",
        "https://open.spotify.com/track/C",
    ]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib = _mock_library(frozenset(urls))

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 0
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert {s["url"] for s in data["songs"]} == set(urls)


# ---------------------------------------------------------------------------
# reconcile_snapshot — one URL missing from library
# ---------------------------------------------------------------------------


def test_reconcile_one_missing_dropped(tmp_path: Path) -> None:
    """A URL absent from both library and quarantine is dropped and logged."""
    urls = [
        "https://open.spotify.com/track/A",
        "https://open.spotify.com/track/B",
        "https://open.spotify.com/track/C",
    ]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib = _mock_library(frozenset([
        "https://open.spotify.com/track/A",
        "https://open.spotify.com/track/B",
    ]))

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 1
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    persisted = {s["url"] for s in data["songs"]}
    assert persisted == {"https://open.spotify.com/track/A", "https://open.spotify.com/track/B"}
    assert "https://open.spotify.com/track/C" not in persisted


def test_reconcile_one_missing_logs_warning(tmp_path: Path, caplog) -> None:
    """A WARNING is emitted for each dropped URL."""
    urls = ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib = _mock_library(frozenset(["https://open.spotify.com/track/A"]))

    with caplog.at_level(logging.WARNING, logger="music_scan.reconcile"):
        reconcile_snapshot(spotdl_file, lib)

    assert any("https://open.spotify.com/track/B" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# reconcile_snapshot — all URLs missing from library
# ---------------------------------------------------------------------------


def test_reconcile_all_missing_drops_all(tmp_path: Path) -> None:
    """When no URLs are in the library or quarantine, all are dropped."""
    urls = ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib = _mock_library(frozenset())

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 2
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert data["songs"] == []


# ---------------------------------------------------------------------------
# reconcile_snapshot — quarantine keeps URLs alive
# ---------------------------------------------------------------------------


def test_reconcile_quarantined_url_not_dropped(tmp_path: Path) -> None:
    """A URL whose file is in quarantine is retained (not re-downloaded)."""
    url_a = "https://open.spotify.com/track/A"
    url_b = "https://open.spotify.com/track/B"
    spotdl_file = _write_snapshot(tmp_path, "mypl", [url_a, url_b])
    lib = _mock_library(frozenset([url_a]))

    dropped = reconcile_snapshot(spotdl_file, lib, safe_urls={url_b})

    assert dropped == 0
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert {s["url"] for s in data["songs"]} == {url_a, url_b}


# ---------------------------------------------------------------------------
# reconcile_snapshot — empty snapshot is a no-op
# ---------------------------------------------------------------------------


def test_reconcile_empty_snapshot_no_op(tmp_path: Path) -> None:
    spotdl_file = tmp_path / "mypl.spotdl"
    spotdl_file.write_text(
        json.dumps({"type": "sync", "query": ["https://..."], "songs": []}),
        encoding="utf-8",
    )
    lib = mock.Mock()

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 0
    lib.spotify_urls_by_source.assert_not_called()


# ---------------------------------------------------------------------------
# reconcile_all — album and nosync playlists are skipped (#186)
# ---------------------------------------------------------------------------


def test_reconcile_all_skips_album_and_nosync_playlists(tmp_path: Path) -> None:
    """Entries of album and nosync playlists that aren't in the library are kept."""
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    synced = _write_snapshot(spotdl_dir, "synced", ["https://open.spotify.com/track/A"])
    album = _write_snapshot(spotdl_dir, "later", ["https://open.spotify.com/track/B"])
    frozen = _write_snapshot(spotdl_dir, "frozen", ["https://open.spotify.com/track/C"])
    conf = tmp_path / "playlists.conf"
    conf.write_text(
        "synced https://open.spotify.com/playlist/s\n"
        "later https://open.spotify.com/playlist/l album\n"
        "frozen https://open.spotify.com/playlist/f nosync\n",
        encoding="utf-8",
    )
    db = tmp_path / "library.db"
    db.touch()
    lib = _mock_library(frozenset())
    lib.__enter__ = mock.Mock(return_value=lib)
    lib.__exit__ = mock.Mock(return_value=False)

    with mock.patch("music_scan.library.MusicLibrary", return_value=lib):
        dropped = reconcile_all(spotdl_dir, db, tmp_path / "quarantine", conf)

    assert dropped == 1
    assert json.loads(synced.read_text(encoding="utf-8"))["songs"] == []
    assert len(json.loads(album.read_text(encoding="utf-8"))["songs"]) == 1
    assert len(json.loads(frozen.read_text(encoding="utf-8"))["songs"]) == 1

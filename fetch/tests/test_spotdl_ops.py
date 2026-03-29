"""Tests for pipeline.spotdl_ops — pure-Python logic (no spotdl process, no network)."""

from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path

import pytest

from pipeline.spotdl_ops import save_playlist, sync_playlist


# ---------------------------------------------------------------------------
# save_playlist
# ---------------------------------------------------------------------------


def test_save_playlist_writes_stub_with_empty_songs(tmp_path: Path) -> None:
    """save_playlist writes a valid sync stub with no songs."""
    spotdl_file = tmp_path / "mypl.spotdl"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    assert spotdl_file.exists()
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert data["type"] == "sync"
    assert data["query"] == ["https://open.spotify.com/playlist/abc"]
    assert data["songs"] == []


def test_save_playlist_overwrites_existing_file(tmp_path: Path) -> None:
    """save_playlist overwrites an existing file without error."""
    spotdl_file = tmp_path / "mypl.spotdl"
    spotdl_file.write_text("old content", encoding="utf-8")

    save_playlist(url="https://open.spotify.com/playlist/xyz", spotdl_file=spotdl_file)

    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert data["query"] == ["https://open.spotify.com/playlist/xyz"]
    assert data["songs"] == []


# ---------------------------------------------------------------------------
# sync_playlist — regression: first sync after provisioning downloads all songs
# ---------------------------------------------------------------------------


def _make_mock_song(url: str, title: str = "Song") -> mock.Mock:
    song = mock.Mock()
    song.url = url
    song.json = {"url": url, "name": title, "artists": ["Artist"]}
    return song


def test_sync_playlist_after_stub_downloads_all_songs(tmp_path: Path) -> None:
    """Regression: after save_playlist writes a stub, sync downloads all songs.

    This is the exact scenario from issue #46: provisioning creates an empty
    snapshot, so the first sync must treat all Spotify songs as 'truly new'.
    """
    spotdl_file = tmp_path / "mypl.spotdl"
    output_dir = tmp_path / "mypl"
    output_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"

    # Simulate provisioning: write the stub
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    # Spotify returns 5 tracks on the first sync
    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(5)]

    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = songs

    with mock.patch("pipeline.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        removed_urls, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    # All 5 songs should be sent to download — none were in the empty snapshot
    assert tracks_sent == 5
    mock_spotdl.download_songs.assert_called_once()
    downloaded = mock_spotdl.download_songs.call_args[0][0]
    assert len(downloaded) == 5
    assert removed_urls == set()


def test_sync_playlist_second_run_skips_known_songs(tmp_path: Path) -> None:
    """On subsequent syncs, songs already in the snapshot are not re-downloaded."""
    spotdl_file = tmp_path / "mypl.spotdl"
    output_dir = tmp_path / "mypl"
    output_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"

    # Simulate a snapshot with 3 previously downloaded songs
    existing_songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(3)]
    spotdl_file.write_text(
        json.dumps({
            "type": "sync",
            "query": ["https://open.spotify.com/playlist/abc"],
            "songs": [s.json for s in existing_songs],
        }),
        encoding="utf-8",
    )

    # Spotify now returns 5 songs: the 3 known + 2 new
    new_songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(5)]

    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = new_songs

    with mock.patch("pipeline.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        removed_urls, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    # Only the 2 new songs should be downloaded
    assert tracks_sent == 2
    downloaded = mock_spotdl.download_songs.call_args[0][0]
    downloaded_urls = {s.url for s in downloaded}
    assert downloaded_urls == {
        "https://open.spotify.com/track/3",
        "https://open.spotify.com/track/4",
    }
    assert removed_urls == set()


def test_sync_playlist_detects_removed_tracks(tmp_path: Path) -> None:
    """Tracks present in the old snapshot but absent from Spotify are flagged as removed."""
    spotdl_file = tmp_path / "mypl.spotdl"
    output_dir = tmp_path / "mypl"
    output_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"

    spotdl_file.write_text(
        json.dumps({
            "type": "sync",
            "query": ["https://open.spotify.com/playlist/abc"],
            "songs": [
                {"url": "https://open.spotify.com/track/A", "name": "Track A", "artists": []},
                {"url": "https://open.spotify.com/track/B", "name": "Track B", "artists": []},
            ],
        }),
        encoding="utf-8",
    )

    # Spotify no longer has track B
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [_make_mock_song("https://open.spotify.com/track/A")]

    with mock.patch("pipeline.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        removed_urls, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    assert removed_urls == {"https://open.spotify.com/track/B"}
    assert tracks_sent == 0  # A was already known; nothing new to download

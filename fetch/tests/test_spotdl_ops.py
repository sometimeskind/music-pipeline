"""Tests for pipeline.spotdl_ops — pure-Python logic (no spotdl process, no network)."""

from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path

import pytest

from music_fetch.spotdl_ops import save_playlist, sync_playlist


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


def _make_mock_song(url: str, title: str = "Song", download_url: str | None = None) -> mock.Mock:
    song = mock.Mock()
    song.url = url
    song.json = {"url": url, "name": title, "artists": ["Artist"]}
    song.download_url = download_url
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
    mock_spotdl.download_songs.return_value = [(s, Path(f"/tmp/{i}.m4a")) for i, s in enumerate(songs)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
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

    # All 5 downloaded songs should be persisted to the snapshot
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert len(data["songs"]) == 5


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

    new_only = [s for s in new_songs if s.url in {"https://open.spotify.com/track/3", "https://open.spotify.com/track/4"}]
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = new_songs
    mock_spotdl.download_songs.return_value = [(s, Path(f"/tmp/{i}.m4a")) for i, s in enumerate(new_only)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
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

    # Snapshot should contain all 3 old + 2 newly downloaded = 5 songs
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert len(data["songs"]) == 5


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
    mock_spotdl.download_songs.return_value = []  # nothing new to download

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        removed_urls, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    assert removed_urls == {"https://open.spotify.com/track/B"}
    assert tracks_sent == 0  # A was already known; nothing new to download

def test_sync_playlist_failed_downloads_not_persisted(tmp_path: Path) -> None:
    """Songs spotdl failed to download (path=None) are excluded from the snapshot.

    Regression for issue #51: previously all attempted songs were written to the
    snapshot regardless of download success, permanently skipping failed tracks.
    """
    spotdl_file = tmp_path / "mypl.spotdl"
    output_dir = tmp_path / "mypl"
    output_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"

    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(4)]

    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = songs
    # Songs 0 and 2 succeed; songs 1 and 3 fail (path=None)
    mock_spotdl.download_songs.return_value = [
        (songs[0], Path("/tmp/0.m4a")),
        (songs[1], None),
        (songs[2], Path("/tmp/2.m4a")),
        (songs[3], None),
    ]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        removed_urls, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    assert tracks_sent == 4  # all 4 were sent to spotdl
    assert removed_urls == set()

    # Only the 2 successful downloads should be in the snapshot
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    persisted_urls = {s["url"] for s in data["songs"]}
    assert persisted_urls == {
        "https://open.spotify.com/track/0",
        "https://open.spotify.com/track/2",
    }
    # Failed tracks (1 and 3) must be absent — they will be retried next run
    assert "https://open.spotify.com/track/1" not in persisted_urls
    assert "https://open.spotify.com/track/3" not in persisted_urls


# ---------------------------------------------------------------------------
# Per-track outcome logging
# ---------------------------------------------------------------------------


def _setup_sync(tmp_path: Path):
    """Return (spotdl_file, output_dir, cookie_file) pointing into tmp_path."""
    spotdl_file = tmp_path / "mypl.spotdl"
    output_dir = tmp_path / "mypl"
    output_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"
    return spotdl_file, output_dir, cookie_file


def test_outcome_ok_logged_for_successful_download(tmp_path: Path, caplog) -> None:
    """[OK] is logged for each track successfully downloaded."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Break Right")
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, Path("/tmp/1.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file)

    assert "[OK]" in caplog.text
    assert "Break Right" in caplog.text


def test_outcome_skip_logged_for_known_track(tmp_path: Path, caplog) -> None:
    """[SKIP] is logged for tracks already present in the snapshot."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    existing = _make_mock_song("https://open.spotify.com/track/1", title="Protocol")
    spotdl_file.write_text(
        json.dumps({"type": "sync", "query": ["https://open.spotify.com/playlist/abc"], "songs": [existing.json]}),
        encoding="utf-8",
    )

    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [existing]
    mock_spotdl.download_songs.return_value = []

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file)

    assert "[SKIP]" in caplog.text
    assert "Protocol" in caplog.text


def test_outcome_miss_logged_when_no_source_found(tmp_path: Path, caplog) -> None:
    """[MISS] is logged when spotdl returns path=None and download_url is unset (LookupError)."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Three Drums", download_url=None)
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file)

    assert "[MISS]" in caplog.text
    assert "Three Drums" in caplog.text
    assert "[FAIL]" not in caplog.text


def test_outcome_fail_logged_when_download_error(tmp_path: Path, caplog) -> None:
    """[FAIL] is logged when spotdl returns path=None but download_url is set (AudioProviderError)."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song(
        "https://open.spotify.com/track/1",
        title="Errored Track",
        download_url="https://www.youtube.com/watch?v=abc",
    )
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file)

    assert "[FAIL]" in caplog.text
    assert "Errored Track" in caplog.text
    assert "[MISS]" not in caplog.text


# ---------------------------------------------------------------------------
# MISS backoff
# ---------------------------------------------------------------------------


def test_miss_track_written_to_failures_file(tmp_path: Path) -> None:
    """A [MISS] track gets a backoff entry in the failures file."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Missing", download_url=None)
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    assert failures_file.exists()
    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert song.url in data
    assert data[song.url]["attempts"] == 1
    assert "retry_after" in data[song.url]


def test_fail_track_not_written_to_failures_file(tmp_path: Path) -> None:
    """A [FAIL] track (download error, not a lookup failure) is not backed off."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", download_url="https://youtube.com/watch?v=x")
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert song.url not in data


def test_miss_track_in_backoff_is_skipped(tmp_path: Path) -> None:
    """A [MISS] track whose retry_after is in the future is not attempted and doesn't consume budget."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    backed_off_url = "https://open.spotify.com/track/backed"
    future = (datetime.now(timezone.utc) + timedelta(days=6)).replace(microsecond=0).isoformat()
    failures_file.write_text(
        json.dumps({backed_off_url: {"attempts": 1, "retry_after": future}}),
        encoding="utf-8",
    )

    backed = _make_mock_song(backed_off_url, title="Backed Off")
    fresh = _make_mock_song("https://open.spotify.com/track/fresh", title="Fresh")
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [backed, fresh]
    mock_spotdl.download_songs.return_value = [(fresh, Path("/tmp/fresh.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file,
            failures_file=failures_file, track_limit=10,
        )

    # Only the fresh track should have been sent
    assert tracks_sent == 1
    downloaded = mock_spotdl.download_songs.call_args[0][0]
    assert all(s.url != backed_off_url for s in downloaded)


def test_miss_track_past_backoff_is_retried(tmp_path: Path) -> None:
    """A [MISS] track whose retry_after has passed is attempted again."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    retry_url = "https://open.spotify.com/track/retry"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    failures_file.write_text(
        json.dumps({retry_url: {"attempts": 1, "retry_after": past}}),
        encoding="utf-8",
    )

    song = _make_mock_song(retry_url, title="Past Backoff", download_url=None)
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file,
        )

    assert tracks_sent == 1
    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert data[retry_url]["attempts"] == 2  # incremented


def test_backoff_schedule() -> None:
    """Backoff grows 7 → 14 → 28 → 28 days across repeated misses."""
    from music_fetch.spotdl_ops import _backoff_days

    assert _backoff_days(1) == 7
    assert _backoff_days(2) == 14
    assert _backoff_days(3) == 28
    assert _backoff_days(4) == 28
    assert _backoff_days(100) == 28


def test_ok_track_removes_failures_entry(tmp_path: Path) -> None:
    """A successful download clears the track's backoff entry."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    track_url = "https://open.spotify.com/track/recovered"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    failures_file.write_text(
        json.dumps({track_url: {"attempts": 2, "retry_after": past}}),
        encoding="utf-8",
    )

    song = _make_mock_song(track_url, title="Recovered")
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, Path("/tmp/recovered.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert track_url not in data


def test_removed_track_clears_failures_entry(tmp_path: Path) -> None:
    """Tracks removed from the Spotify playlist are pruned from the failures file."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"

    removed_url = "https://open.spotify.com/track/gone"
    spotdl_file.write_text(
        json.dumps({
            "type": "sync",
            "query": ["https://open.spotify.com/playlist/abc"],
            "songs": [{"url": removed_url, "name": "Gone", "artists": []}],
        }),
        encoding="utf-8",
    )
    failures_file.write_text(
        json.dumps({removed_url: {"attempts": 1, "retry_after": "2099-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )

    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = []  # track removed from Spotify
    mock_spotdl.download_songs.return_value = []

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert removed_url not in data


def test_corrupt_failures_file_treated_as_empty(tmp_path: Path) -> None:
    """A corrupt or unreadable failures file is handled gracefully — treated as empty."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)
    failures_file.write_text("not valid json", encoding="utf-8")

    song = _make_mock_song("https://open.spotify.com/track/1", title="Normal")
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, Path("/tmp/1.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, tracks_sent = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file,
        )

    assert tracks_sent == 1  # proceeded normally despite corrupt file


def test_outcome_defer_logged_for_budget_limited_tracks(tmp_path: Path, caplog) -> None:
    """[DEFER] is logged for tracks not attempted due to track budget."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}", title=f"Track {i}") for i in range(3)]
    mock_spotdl = mock.Mock()
    mock_spotdl.search.return_value = songs
    mock_spotdl.download_songs.return_value = [(songs[0], Path("/tmp/0.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, track_limit=1)

    assert "[DEFER]" in caplog.text
    assert "Track 1" in caplog.text
    assert "Track 2" in caplog.text

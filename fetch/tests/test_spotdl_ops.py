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


def _make_mock_song(url: str, title: str = "Song") -> mock.Mock:
    song = mock.Mock()
    song.url = url
    song.json = {"url": url, "name": title, "artists": ["Artist"]}
    return song


def _lookup_error(url: str) -> str:
    """The Downloader.errors entry spotdl records when no YouTube source is found."""
    return f"{url} - LookupError: No results found for song: Artist - Song"


def _download_error(url: str) -> str:
    """The Downloader.errors entry spotdl records when a source was found but yt-dlp failed."""
    return f"{url} - AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=abc"


def _mock_spotdl(songs: list, results: list, errors: list[str] = ()) -> mock.Mock:
    """Spotdl double whose download_songs() records *errors* the way spotdl does.

    spotdl appends to Downloader.errors during download_songs(); sync_playlist clears
    the list right before the call, so the errors must appear during the call, not before.
    """
    m = mock.Mock()
    m.search.return_value = songs
    m.downloader.errors = []

    def _download(_batch):
        m.downloader.errors.extend(errors)
        return results

    m.download_songs.side_effect = _download
    return m


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
        removed_urls, attempted, downloaded, _missed, _failed = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    # All 5 songs should be sent to download — none were in the empty snapshot
    assert attempted == 5
    assert downloaded == 5
    mock_spotdl.download_songs.assert_called_once()
    sent_to_spotdl = mock_spotdl.download_songs.call_args[0][0]
    assert len(sent_to_spotdl) == 5
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
        removed_urls, attempted, downloaded, _missed, _failed = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    # Only the 2 new songs should be downloaded
    assert attempted == 2
    assert downloaded == 2
    sent_to_spotdl = mock_spotdl.download_songs.call_args[0][0]
    sent_urls = {s.url for s in sent_to_spotdl}
    assert sent_urls == {
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
        removed_urls, attempted, downloaded, _missed, _failed = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    assert removed_urls == {"https://open.spotify.com/track/B"}
    assert attempted == 0  # A was already known; nothing new to download
    assert downloaded == 0

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

    # Songs 0 and 2 succeed; song 1 has no source (MISS), song 3 fails to download (FAIL)
    mock_spotdl = _mock_spotdl(
        songs,
        [
            (songs[0], Path("/tmp/0.m4a")),
            (songs[1], None),
            (songs[2], Path("/tmp/2.m4a")),
            (songs[3], None),
        ],
        errors=[_lookup_error(songs[1].url), _download_error(songs[3].url)],
    )

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        removed_urls, attempted, downloaded, n_missed, n_failed = sync_playlist(
            spotdl_file=spotdl_file,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    assert attempted == 4  # all 4 were sent to spotdl
    assert downloaded == 2  # only 2 actually landed on disk (regression for issue #124)
    assert n_missed == 1
    assert n_failed == 1
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
    """[MISS] is logged when spotdl returns path=None and recorded a LookupError for the track."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Three Drums")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_lookup_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file)

    assert "[MISS]" in caplog.text
    assert "Three Drums" in caplog.text
    assert "[FAIL]" not in caplog.text


def test_outcome_fail_logged_when_download_error(tmp_path: Path, caplog) -> None:
    """[FAIL] is logged when spotdl returns path=None and recorded an AudioProviderError (issue #151)."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Errored Track")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_download_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        _, _, _, n_missed, n_failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file,
        )

    assert n_failed == 1
    assert n_missed == 0
    assert "[FAIL]" in caplog.text
    assert "Errored Track" in caplog.text
    assert "AudioProviderError: YT-DLP download error" in caplog.text
    assert "[MISS]" not in caplog.text


def test_path_none_without_error_is_fail(tmp_path: Path, caplog) -> None:
    """path=None with no recorded error is a [FAIL] — never back off as MISS without evidence."""
    import logging
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Silent")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        _, _, _, n_missed, n_failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file,
        )

    assert (n_missed, n_failed) == (0, 1)
    assert "[FAIL]" in caplog.text
    assert "[MISS]" not in caplog.text


def test_downloader_errors_missing_treated_as_fail(tmp_path: Path) -> None:
    """A Spotdl object without a list-shaped downloader.errors still classifies (as FAIL)."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1")
    mock_spotdl = mock.Mock()  # downloader.errors is a Mock, not a list
    mock_spotdl.search.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, _, _, n_missed, n_failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file,
        )

    assert (n_missed, n_failed) == (0, 1)


def test_stale_errors_cleared_before_download(tmp_path: Path) -> None:
    """Errors left in downloader.errors by a previous playlist (singleton Spotdl) don't leak."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[])
    mock_spotdl.downloader.errors.append(_lookup_error(song.url))  # stale, from an earlier call

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, _, _, n_missed, n_failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file,
        )

    assert (n_missed, n_failed) == (0, 1)
    assert mock_spotdl.downloader.errors == []


def test_downloader_settings_do_not_force_js_runtime() -> None:
    """yt-dlp must pick its default JS runtime (Deno on PATH); no --js-runtimes override (issue #151)."""
    from music_fetch.spotdl_ops import _make_downloader_settings

    settings = _make_downloader_settings(cookie_file=Path("/tmp/cookies.txt"))
    assert "yt_dlp_args" not in settings


# ---------------------------------------------------------------------------
# MISS backoff
# ---------------------------------------------------------------------------


def test_miss_track_written_to_failures_file(tmp_path: Path) -> None:
    """A [MISS] track gets a backoff entry in the failures file."""
    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Missing")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_lookup_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    assert failures_file.exists()
    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert song.url in data
    assert data[song.url]["kind"] == "miss"
    assert data[song.url]["attempts"] == 1
    assert "retry_after" in data[song.url]


def test_fail_track_written_with_fail_kind(tmp_path: Path) -> None:
    """A [FAIL] track gets a short 'fail' backoff entry, not the 7-day MISS one (issue #151)."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    song = _make_mock_song("https://open.spotify.com/track/1")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_download_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert data[song.url]["kind"] == "fail"
    assert data[song.url]["attempts"] == 1
    retry_after = datetime.fromisoformat(data[song.url]["retry_after"])
    assert timedelta(hours=23) < retry_after - datetime.now(timezone.utc) <= timedelta(days=1)


def test_fail_track_in_backoff_is_skipped(tmp_path: Path) -> None:
    """A 'fail' entry whose retry_after is in the future is not attempted."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    url = "https://open.spotify.com/track/failing"
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).replace(microsecond=0).isoformat()
    failures_file.write_text(
        json.dumps({url: {"kind": "fail", "attempts": 1, "retry_after": future}}),
        encoding="utf-8",
    )

    song = _make_mock_song(url)
    mock_spotdl = _mock_spotdl([song], [], errors=[])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, attempted, _, _, _ = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file,
        )

    assert attempted == 0
    assert mock_spotdl.download_songs.call_args[0][0] == []


def test_kind_change_resets_attempts(tmp_path: Path) -> None:
    """When a track flips from MISS to FAIL (or back), attempts restart under the new kind."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    url = "https://open.spotify.com/track/flip"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    failures_file.write_text(
        json.dumps({url: {"kind": "miss", "attempts": 3, "retry_after": past}}),
        encoding="utf-8",
    )

    song = _make_mock_song(url)
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_download_error(url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert data[url]["kind"] == "fail"
    assert data[url]["attempts"] == 1


def test_legacy_entry_without_kind_is_miss(tmp_path: Path) -> None:
    """Entries written before the 'kind' field existed are treated as MISS and keep counting."""
    from datetime import datetime, timedelta, timezone

    spotdl_file, output_dir, cookie_file = _setup_sync(tmp_path)
    failures_file = tmp_path / ".spotdl-failures.json"
    save_playlist(url="https://open.spotify.com/playlist/abc", spotdl_file=spotdl_file)

    url = "https://open.spotify.com/track/legacy"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    failures_file.write_text(json.dumps({url: {"attempts": 2, "retry_after": past}}), encoding="utf-8")

    song = _make_mock_song(url)
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_lookup_error(url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        sync_playlist(spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file)

    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert data[url]["kind"] == "miss"
    assert data[url]["attempts"] == 3


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
        _, attempted, downloaded, _missed, _failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file,
            failures_file=failures_file, track_limit=10,
        )

    # Only the fresh track should have been sent
    assert attempted == 1
    assert downloaded == 1
    sent_to_spotdl = mock_spotdl.download_songs.call_args[0][0]
    assert all(s.url != backed_off_url for s in sent_to_spotdl)


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

    song = _make_mock_song(retry_url, title="Past Backoff")
    mock_spotdl = _mock_spotdl([song], [(song, None)], errors=[_lookup_error(retry_url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _, attempted, downloaded, _missed, _failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file,
        )

    assert attempted == 1
    assert downloaded == 0  # spotdl returned path=None — must NOT be counted as downloaded
    data = json.loads(failures_file.read_text(encoding="utf-8"))
    assert data[retry_url]["attempts"] == 2  # incremented


def test_backoff_schedule() -> None:
    """MISS backoff grows 7 → 14 → 28 → 28 days; FAIL backoff grows 1 → 2 → 4 → 4 days."""
    from music_fetch.spotdl_ops import _backoff_days

    assert [_backoff_days(n, "miss") for n in (1, 2, 3, 4, 100)] == [7, 14, 28, 28, 28]
    assert [_backoff_days(n, "fail") for n in (1, 2, 3, 4, 100)] == [1, 2, 4, 4, 4]
    assert _backoff_days(1) == 7  # default kind is miss


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
        _, attempted, downloaded, _missed, _failed = sync_playlist(
            spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=cookie_file, failures_file=failures_file,
        )

    assert attempted == 1  # proceeded normally despite corrupt file
    assert downloaded == 1


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

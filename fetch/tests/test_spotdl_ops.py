"""Tests for pipeline.spotdl_ops — pure-Python logic (no spotdl process, no network)."""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest

from music_fetch import state
from music_fetch.spotdl_ops import sync_playlist

PLAYLIST_URL = "https://open.spotify.com/playlist/abc"


def _backoff() -> dict:
    """The spotdl backoff as the database holds it."""
    with state.session() as conn:
        return state.load_failures(conn)


def _set_backoff(failures: dict) -> None:
    with state.session() as conn:
        state.save_failures(conn, failures)


@pytest.fixture(autouse=True)
def _no_pins(tmp_path: Path):
    """Keep every download batch off the pod's track-pins.conf (#247): CI runs
    as a user who can't even stat /root."""
    with mock.patch("music_fetch.spotdl_ops.PINS_FILE", tmp_path / "track-pins.conf"):
        yield


# ---------------------------------------------------------------------------
# sync_playlist — the want list from the store's previous songs (homelab#1914)
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_songs():
    """The playlist read (get_simple_songs); set .return_value to the playlist's songs."""
    with mock.patch("spotdl.utils.search.get_simple_songs", return_value=[]) as m:
        yield m


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


def _mock_spotdl(results: list, errors: list[str] = ()) -> mock.Mock:
    """Spotdl double whose download_songs() records *errors* the way spotdl does.

    spotdl appends to Downloader.errors during download_songs(); sync_playlist clears
    the list right before the call, so the errors must appear during the call, not before.
    """
    m = mock.Mock()
    m.downloader.errors = []

    def _download(_batch):
        m.downloader.errors.extend(errors)
        return results

    m.download_songs.side_effect = _download
    return m


def _not_in(previous) -> callable:
    """The store's want test when the library holds every *previous* song and nothing else."""
    held = {s.get("url") for s in previous}
    return lambda song: song["url"] not in held


def _sync(output_dir: Path, cookie_file: Path, previous=(), wants=None, **kwargs):
    """sync_playlist on PLAYLIST_URL; unless *wants* is given, the store wants
    exactly the tracks *previous* lacks."""
    previous = list(previous)
    return sync_playlist(
        PLAYLIST_URL, "mypl", previous, wants or _not_in(previous), output_dir, cookie_file, **kwargs,
    )


def _setup_sync(tmp_path: Path):
    """Return (output_dir, cookie_file) pointing into tmp_path."""
    output_dir = tmp_path / "mypl"
    output_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"
    return output_dir, cookie_file


def test_sync_playlist_first_sync_downloads_all_songs(tmp_path: Path, simple_songs) -> None:
    """Regression (#46): a new playlist has an empty store, so the first sync
    downloads every Spotify song."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    # Spotify returns 5 tracks on the first sync
    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(5)]

    mock_spotdl = mock.Mock()
    simple_songs.return_value = songs
    mock_spotdl.download_songs.return_value = [(s, Path(f"/tmp/{i}.m4a")) for i, s in enumerate(songs)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file)

    simple_songs.assert_called_once_with([PLAYLIST_URL])
    # All 5 songs should be sent to download — none were in the empty store
    assert result.attempted == 5
    assert result.downloaded == 5
    mock_spotdl.download_songs.assert_called_once()
    sent_to_spotdl = mock_spotdl.download_songs.call_args[0][0]
    assert len(sent_to_spotdl) == 5
    assert result.removed_urls == set()

    # All 5 downloaded songs come back for the store
    assert result.songs == [s.json for s in songs]
    assert result.records == {s.url: s.json for s in songs}


def test_sync_playlist_second_run_skips_known_songs(tmp_path: Path, simple_songs) -> None:
    """On subsequent syncs, songs the store already holds are not re-downloaded."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    # The store holds 3 previously downloaded songs
    existing_songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(3)]

    # Spotify now returns 5 songs: the 3 known + 2 new
    new_songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(5)]

    new_only = [s for s in new_songs if s.url in {"https://open.spotify.com/track/3", "https://open.spotify.com/track/4"}]
    mock_spotdl = mock.Mock()
    simple_songs.return_value = new_songs
    mock_spotdl.download_songs.return_value = [(s, Path(f"/tmp/{i}.m4a")) for i, s in enumerate(new_only)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=[s.json for s in existing_songs])

    # Only the 2 new songs should be downloaded
    assert result.attempted == 2
    assert result.downloaded == 2
    sent_to_spotdl = mock_spotdl.download_songs.call_args[0][0]
    sent_urls = {s.url for s in sent_to_spotdl}
    assert sent_urls == {
        "https://open.spotify.com/track/3",
        "https://open.spotify.com/track/4",
    }
    assert result.removed_urls == set()

    # Records cover all 3 old + 2 newly downloaded = 5 songs
    assert set(result.records) == {s.url for s in new_songs}
    assert len(result.songs) == 5


def test_sync_playlist_detects_removed_tracks(tmp_path: Path, simple_songs) -> None:
    """Tracks in the previous store but absent from Spotify are flagged as removed."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    previous = [
        {"url": "https://open.spotify.com/track/A", "name": "Track A", "artists": []},
        {"url": "https://open.spotify.com/track/B", "name": "Track B", "artists": []},
    ]

    # Spotify no longer has track B
    mock_spotdl = mock.Mock()
    simple_songs.return_value = [_make_mock_song("https://open.spotify.com/track/A")]
    mock_spotdl.download_songs.return_value = []  # nothing new to download

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=previous)

    assert result.removed_urls == {"https://open.spotify.com/track/B"}
    assert result.attempted == 0  # A was already known; nothing new to download
    assert result.downloaded == 0
    # B is gone from the playlist's songs; the records still look up every previous one
    assert result.songs == [{"url": "https://open.spotify.com/track/A", "name": "Song", "artists": ["Artist"]}]
    assert result.records == {s["url"]: s for s in previous}


def test_sync_playlist_failed_downloads_have_no_record(tmp_path: Path, simple_songs) -> None:
    """Songs spotdl failed to download (path=None) get no record, so the store
    still wants them next run.

    Regression for issue #51: previously all attempted songs were recorded
    regardless of download success, permanently skipping failed tracks.
    """
    output_dir, cookie_file = _setup_sync(tmp_path)

    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(4)]

    # Songs 0 and 2 succeed; song 1 has no source (MISS), song 3 fails to download (FAIL)
    simple_songs.return_value = songs
    mock_spotdl = _mock_spotdl(
        [
            (songs[0], Path("/tmp/0.m4a")),
            (songs[1], None),
            (songs[2], Path("/tmp/2.m4a")),
            (songs[3], None),
        ],
        errors=[_lookup_error(songs[1].url), _download_error(songs[3].url)],
    )

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file)

    assert result.attempted == 4  # all 4 were sent to spotdl
    assert result.downloaded == 2  # only 2 actually landed on disk (regression for issue #124)
    assert result.missed == 1
    assert result.failed == 1
    assert result.removed_urls == set()

    # Only the 2 successful downloads have a record
    assert set(result.records) == {
        "https://open.spotify.com/track/0",
        "https://open.spotify.com/track/2",
    }
    # Failed tracks (1 and 3) are still on the playlist, with only their page record
    assert [s["url"] for s in result.songs] == [s.url for s in songs]


# ---------------------------------------------------------------------------
# Per-track outcome logging
# ---------------------------------------------------------------------------

def test_outcome_ok_logged_for_successful_download(tmp_path: Path, caplog, simple_songs) -> None:
    """[OK] is logged for each track successfully downloaded."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Break Right")
    mock_spotdl = mock.Mock()
    simple_songs.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, Path("/tmp/1.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        _sync(output_dir=output_dir, cookie_file=cookie_file)

    assert "[OK]" in caplog.text
    assert "Break Right" in caplog.text


def test_known_unwanted_track_is_counted_not_logged(tmp_path: Path, caplog, simple_songs) -> None:
    """A track the store holds and does not want is counted in the playlist's
    summary line; its [SKIP] line is DEBUG (#320)."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)
    existing = _make_mock_song("https://open.spotify.com/track/1", title="Protocol")

    mock_spotdl = mock.Mock()
    simple_songs.return_value = [existing]
    mock_spotdl.download_songs.return_value = []

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        _sync(output_dir=output_dir, cookie_file=cookie_file, previous=[existing.json])

    assert "mypl: 1 track(s) on Spotify, 1 held or waiting, 0 wanted (0 on the playlist before, 0 new), 0 removed" in caplog.text
    assert "[SKIP]" not in caplog.text
    assert "Protocol" not in caplog.text


def test_outcome_miss_logged_when_no_source_found(tmp_path: Path, caplog, simple_songs) -> None:
    """[MISS] is logged when spotdl returns path=None and recorded a LookupError for the track."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Three Drums")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_lookup_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        _sync(output_dir=output_dir, cookie_file=cookie_file)

    assert "[MISS]" in caplog.text
    assert "Three Drums" in caplog.text
    assert "[FAIL]" not in caplog.text


def test_outcome_fail_logged_when_download_error(tmp_path: Path, caplog, simple_songs) -> None:
    """[FAIL] is logged when spotdl returns path=None and recorded an AudioProviderError (issue #151)."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Errored Track")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_download_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file,
        )

    assert result.failed == 1
    assert result.missed == 0
    assert "[FAIL]" in caplog.text
    assert "Errored Track" in caplog.text
    assert "AudioProviderError: YT-DLP download error" in caplog.text
    assert "[MISS]" not in caplog.text


def test_path_none_without_error_is_fail(tmp_path: Path, caplog, simple_songs) -> None:
    """path=None with no recorded error is a [FAIL] — never back off as MISS without evidence."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Silent")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file,
        )

    assert (result.missed, result.failed) == (0, 1)
    assert "[FAIL]" in caplog.text
    assert "[MISS]" not in caplog.text


def test_downloader_errors_missing_treated_as_fail(tmp_path: Path, simple_songs) -> None:
    """A Spotdl object without a list-shaped downloader.errors still classifies (as FAIL)."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1")
    mock_spotdl = mock.Mock()  # downloader.errors is a Mock, not a list
    simple_songs.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, None)]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file,
        )

    assert (result.missed, result.failed) == (0, 1)


def test_stale_errors_cleared_before_download(tmp_path: Path, simple_songs) -> None:
    """Errors left in downloader.errors by a previous playlist (singleton Spotdl) don't leak."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[])
    mock_spotdl.downloader.errors.append(_lookup_error(song.url))  # stale, from an earlier call

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file,
        )

    assert (result.missed, result.failed) == (0, 1)
    assert mock_spotdl.downloader.errors == []


def test_downloader_settings_do_not_force_js_runtime() -> None:
    """yt-dlp must pick its default JS runtime (Deno on PATH); no --js-runtimes override (issue #151)."""
    from music_fetch.spotdl_ops import _make_downloader_settings

    settings = _make_downloader_settings(cookie_file=Path("/tmp/cookies.txt"))
    assert "yt_dlp_args" not in settings


# ---------------------------------------------------------------------------
# MISS backoff
# ---------------------------------------------------------------------------


def test_miss_track_gets_a_backoff_entry(tmp_path: Path, simple_songs) -> None:
    """A [MISS] track gets a backoff entry."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1", title="Missing")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_lookup_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)
    data = _backoff()
    assert song.url in data
    assert data[song.url]["kind"] == "miss"
    assert data[song.url]["attempts"] == 1
    assert "retry_after" in data[song.url]


def test_fail_track_written_with_fail_kind(tmp_path: Path, simple_songs) -> None:
    """A [FAIL] track gets a short 'fail' backoff entry, not the 7-day MISS one (issue #151)."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_download_error(song.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    data = _backoff()
    assert data[song.url]["kind"] == "fail"
    assert data[song.url]["attempts"] == 1
    retry_after = datetime.fromisoformat(data[song.url]["retry_after"])
    assert timedelta(hours=23) < retry_after - datetime.now(timezone.utc) <= timedelta(days=1)


def test_fail_track_in_backoff_is_skipped(tmp_path: Path, simple_songs) -> None:
    """A 'fail' entry whose retry_after is in the future is not attempted."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    url = "https://open.spotify.com/track/failing"
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).replace(microsecond=0).isoformat()
    _set_backoff({url: {"kind": "fail", "attempts": 1, "retry_after": future}})

    song = _make_mock_song(url)
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([], errors=[])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file, backoff=True,
        )

    assert result.attempted == 0
    mock_spotdl.download_songs.assert_not_called()  # an empty batch makes no spotdl call (#228)


def test_kind_change_resets_attempts(tmp_path: Path, simple_songs) -> None:
    """When a track flips from MISS to FAIL (or back), attempts restart under the new kind."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    url = "https://open.spotify.com/track/flip"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    _set_backoff({url: {"kind": "miss", "attempts": 3, "retry_after": past}})

    song = _make_mock_song(url)
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_download_error(url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    data = _backoff()
    assert data[url]["kind"] == "fail"
    assert data[url]["attempts"] == 1


def test_legacy_entry_without_kind_is_miss(tmp_path: Path, simple_songs) -> None:
    """Entries written before the 'kind' field existed are treated as MISS and keep counting."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    url = "https://open.spotify.com/track/legacy"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    _set_backoff({url: {"attempts": 2, "retry_after": past}})

    song = _make_mock_song(url)
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_lookup_error(url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    data = _backoff()
    assert data[url]["kind"] == "miss"
    assert data[url]["attempts"] == 3


def test_miss_track_in_backoff_is_skipped(tmp_path: Path, simple_songs, caplog) -> None:
    """A [MISS] track whose retry_after is in the future is not attempted and doesn't
    consume budget; backed-off tracks get one [BACK] count line, not one each (#320)."""
    import logging
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    backed_off_url = "https://open.spotify.com/track/backed"
    future = (datetime.now(timezone.utc) + timedelta(days=6)).replace(microsecond=0).isoformat()
    _set_backoff({backed_off_url: {"attempts": 1, "retry_after": future}})

    backed = _make_mock_song(backed_off_url, title="Backed Off")
    fresh = _make_mock_song("https://open.spotify.com/track/fresh", title="Fresh")
    mock_spotdl = mock.Mock()
    simple_songs.return_value = [backed, fresh]
    mock_spotdl.download_songs.return_value = [(fresh, Path("/tmp/fresh.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file,
            backoff=True, track_limit=10,
        )

    # Only the fresh track should have been sent
    assert result.attempted == 1
    assert result.downloaded == 1
    sent_to_spotdl = mock_spotdl.download_songs.call_args[0][0]
    assert all(s.url != backed_off_url for s in sent_to_spotdl)
    assert f"[BACK] 1 track(s) backed off (miss 1), next retry {future[:10]}" in caplog.text
    assert "Backed Off" not in caplog.text


def test_miss_track_past_backoff_is_retried(tmp_path: Path, simple_songs) -> None:
    """A [MISS] track whose retry_after has passed is attempted again."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    retry_url = "https://open.spotify.com/track/retry"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    _set_backoff({retry_url: {"attempts": 1, "retry_after": past}})

    song = _make_mock_song(retry_url, title="Past Backoff")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl([(song, None)], errors=[_lookup_error(retry_url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file, backoff=True,
        )

    assert result.attempted == 1
    assert result.downloaded == 0  # spotdl returned path=None — must NOT be counted as downloaded
    data = _backoff()
    assert data[retry_url]["attempts"] == 2  # incremented


def test_backoff_schedule() -> None:
    """MISS backoff grows 7 → 14 → 28 → 28 days; FAIL backoff grows 1 → 2 → 4 → 4 days."""
    from music_fetch.spotdl_ops import _backoff_days

    assert [_backoff_days(n, "miss") for n in (1, 2, 3, 4, 100)] == [7, 14, 28, 28, 28]
    assert [_backoff_days(n, "fail") for n in (1, 2, 3, 4, 100)] == [1, 2, 4, 4, 4]
    assert _backoff_days(1) == 7  # default kind is miss


def test_ok_track_removes_failures_entry(tmp_path: Path, simple_songs) -> None:
    """A successful download clears the track's backoff entry."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    track_url = "https://open.spotify.com/track/recovered"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    _set_backoff({track_url: {"attempts": 2, "retry_after": past}})

    song = _make_mock_song(track_url, title="Recovered")
    mock_spotdl = mock.Mock()
    simple_songs.return_value = [song]
    mock_spotdl.download_songs.return_value = [(song, Path("/tmp/recovered.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    data = _backoff()
    assert track_url not in data


def test_removed_track_clears_failures_entry(tmp_path: Path, simple_songs) -> None:
    """Tracks removed from the Spotify playlist are pruned from the backoff."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    removed_url = "https://open.spotify.com/track/gone"
    previous = [{"url": removed_url, "name": "Gone", "artists": []}]
    _set_backoff({removed_url: {"attempts": 1, "retry_after": "2099-01-01T00:00:00+00:00"}})

    mock_spotdl = mock.Mock()
    simple_songs.return_value = []  # track removed from Spotify
    mock_spotdl.download_songs.return_value = []

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=previous, backoff=True)

    assert result.removed_urls == {removed_url}
    data = _backoff()
    assert removed_url not in data


def test_budget_limited_tracks_are_counted_not_logged(tmp_path: Path, caplog, simple_songs) -> None:
    """Tracks over the budget are counted in the budget line; their [DEFER] lines are DEBUG (#320)."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)

    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}", title=f"Track {i}") for i in range(3)]
    mock_spotdl = mock.Mock()
    simple_songs.return_value = songs
    mock_spotdl.download_songs.return_value = [(songs[0], Path("/tmp/0.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        _sync(output_dir=output_dir, cookie_file=cookie_file, track_limit=1)

    assert "Track budget: downloading 1 of 3 new track(s) this session (2 deferred to next run)" in caplog.text
    assert "[DEFER]" not in caplog.text
    assert "Track 1" not in caplog.text


def test_sync_reads_pages_only_and_keeps_known_entries(tmp_path: Path, simple_songs) -> None:
    """Spotdl.search() re-fetched every track on every run (#183); known tracks keep their record."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    url = "https://open.spotify.com/track/{}".format
    old = [{"url": url(n), "name": f"Old {n}", "genres": ["rock"]} for n in range(3)]

    # Playlist order: 0, new, 1, deferred, 2.  The page data is thinner than the store's records.
    page = {n: _make_mock_song(url(n), title=f"Page {n}") for n in (0, "new", 1, "deferred", 2)}
    simple_songs.return_value = list(page.values())
    full_new = _make_mock_song(url("new"), title="Full new")
    full_new.json["genres"] = ["pop"]
    mock_spotdl = mock.Mock()
    mock_spotdl.search.side_effect = AssertionError("per-track playlist read")
    mock_spotdl.download_songs.return_value = [(full_new, Path("/tmp/new.m4a"))]

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=old, track_limit=1)

    simple_songs.assert_called_once_with([PLAYLIST_URL])
    mock_spotdl.download_songs.assert_called_once_with([page["new"]])
    assert (result.attempted, result.downloaded) == (1, 1)
    # Every page record, in playlist order; the deferred track has no record yet.
    assert result.songs == [page[n].json for n in (0, "new", 1, "deferred", 2)]
    assert result.records == {old[0]["url"]: old[0], url("new"): full_new.json,
                              old[1]["url"]: old[1], old[2]["url"]: old[2]}


# ---------------------------------------------------------------------------
# Per-track failure reasons: chained cause, SyncResult, failures-file round-trip (issue #159)
# ---------------------------------------------------------------------------


class _AudioProviderError(Exception):
    """Stand-in for spotdl.providers.audio.base.AudioProviderError."""


class _DownloadError(Exception):
    """Stand-in for yt_dlp.utils.DownloadError."""


class _HTTPError(Exception):
    """Stand-in for urllib.error.HTTPError."""


def _chained_403(video_url: str = "https://music.youtube.com/watch?v=abc") -> Exception:
    """Build the exception chain spotdl produces when yt-dlp gets a 403 on the media URL."""
    try:
        try:
            raise _HTTPError("HTTP Error 403: Forbidden")
        except _HTTPError:
            # yt-dlp re-raises from inside the except block: implicit chaining (__context__ only)
            raise _DownloadError("ERROR: unable to download video data:\nHTTP Error 403: Forbidden")
    except _DownloadError as exc:
        try:
            raise _AudioProviderError(f"YT-DLP download error - {video_url}") from exc
        except _AudioProviderError as outer:
            return outer
    raise AssertionError("unreachable")


def _mock_spotdl_with_causes(results: list, causes: dict[str, BaseException]) -> mock.Mock:
    """Spotdl double that reports failures the way spotdl 4.5.2 does.

    For every song in *causes* download_songs() hands the exception object to the song's
    progress tracker via notify_error() and appends the formatted string to
    Downloader.errors.  Only the string survives in Downloader.errors; the chained cause
    is reachable only through notify_error().
    """
    m = mock.Mock()
    m.downloader.errors = []
    m.downloader.progress_handler.get_new_tracker.side_effect = lambda song: mock.Mock()

    def _download(batch):
        for song in batch:
            exc = causes.get(song.url)
            if exc is None:
                continue
            tracker = m.downloader.progress_handler.get_new_tracker(song)
            tracker.notify_error("Traceback (most recent call last): ...", exc, True)
            m.downloader.errors.append(f"{song.url} - {exc.__class__.__name__}: {exc}")
        return results

    m.download_songs.side_effect = _download
    return m


def test_download_error_reasons_append_chained_cause() -> None:
    """The reason string carries every link of the exception chain, on one line."""
    from music_fetch.spotdl_ops import _download_error_reasons

    url = "https://open.spotify.com/track/1"
    exc = _chained_403()
    reasons = _download_error_reasons([f"{url} - {exc.__class__.__name__}: {exc}"], {url: exc})

    assert reasons[url] == (
        "_AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=abc "
        "(caused by _DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden; "
        "_HTTPError: HTTP Error 403: Forbidden)"
    )
    assert "\n" not in reasons[url]


def test_download_error_reasons_without_cause_unchanged() -> None:
    """No captured exception (or an unchained one) leaves the spotdl string as-is."""
    from music_fetch.spotdl_ops import _download_error_reasons

    url = "https://open.spotify.com/track/1"
    expected = "AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=abc"
    plain = _AudioProviderError("YT-DLP download error - https://music.youtube.com/watch?v=abc")
    assert _download_error_reasons([_download_error(url)], {url: plain})[url] == expected
    assert _download_error_reasons([_download_error(url)], {})[url] == expected
    assert _download_error_reasons([_download_error(url)], None)[url] == expected


def test_sync_playlist_returns_fail_reasons_with_cause(tmp_path: Path, caplog, simple_songs) -> None:
    """sync_playlist exposes the per-track [FAIL] reason (with chained cause) to the caller."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)

    ok = _make_mock_song("https://open.spotify.com/track/ok")
    missing = _make_mock_song("https://open.spotify.com/track/miss")
    failed = _make_mock_song("https://open.spotify.com/track/fail", title="Forbidden")
    simple_songs.return_value = [ok, missing, failed]
    mock_spotdl = _mock_spotdl_with_causes(
        [(ok, Path("/tmp/ok.m4a")), (missing, None), (failed, None)],
        causes={missing.url: LookupError("No results found for song: Artist - Song"), failed.url: _chained_403()},
    )

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file)

    assert (result.attempted, result.downloaded, result.missed, result.failed) == (3, 1, 1, 1)
    assert set(result.fail_reasons) == {failed.url}  # [MISS] tracks are not download failures
    assert "HTTP Error 403: Forbidden" in result.fail_reasons[failed.url]
    assert "[FAIL] Artist - Forbidden → _AudioProviderError: YT-DLP download error" in caplog.text
    assert "(caused by _DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden" in caplog.text


def test_cause_capture_installed_once_on_singleton(tmp_path: Path, simple_songs) -> None:
    """The progress-handler hook is installed once per Spotdl instance and reset per batch."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    song = _make_mock_song("https://open.spotify.com/track/1")
    simple_songs.return_value = [song]
    mock_spotdl = _mock_spotdl_with_causes([(song, None)], causes={song.url: _chained_403()})
    original_get_new_tracker = mock_spotdl.downloader.progress_handler.get_new_tracker

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        first = _sync(output_dir=output_dir, cookie_file=cookie_file)
        wrapped = mock_spotdl.downloader.progress_handler.get_new_tracker
        # Second run: the song is still new (never downloaded) and fails again.
        second = _sync(output_dir=output_dir, cookie_file=cookie_file)

    assert wrapped is not original_get_new_tracker
    assert mock_spotdl.downloader.progress_handler.get_new_tracker is wrapped  # not re-wrapped
    assert "HTTP Error 403" in first.fail_reasons[song.url]
    assert "HTTP Error 403" in second.fail_reasons[song.url]


def test_backoff_round_trips_reason(tmp_path: Path, simple_songs) -> None:
    """[FAIL] and [MISS] entries persist their reason so investigations don't need a pod exec."""
    output_dir, cookie_file = _setup_sync(tmp_path)

    missing = _make_mock_song("https://open.spotify.com/track/miss")
    failed = _make_mock_song("https://open.spotify.com/track/fail")
    simple_songs.return_value = [missing, failed]
    mock_spotdl = _mock_spotdl_with_causes(
        [(missing, None), (failed, None)],
        causes={missing.url: LookupError("No results found for song: Artist - Song"), failed.url: _chained_403()},
    )

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    data = _backoff()
    assert data[missing.url]["kind"] == "miss"
    assert data[missing.url]["reason"] == "LookupError: No results found for song: Artist - Song"
    assert data[failed.url]["kind"] == "fail"
    assert data[failed.url]["reason"].startswith("_AudioProviderError: YT-DLP download error")
    assert "(caused by _DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden" in data[failed.url]["reason"]


def test_backoff_legacy_entry_without_reason(tmp_path: Path, simple_songs) -> None:
    """Entries written before 'reason' existed still load, back off, and gain a reason on the next failure."""
    from datetime import datetime, timedelta, timezone

    output_dir, cookie_file = _setup_sync(tmp_path)

    backed_off = _make_mock_song("https://open.spotify.com/track/backed")
    due = _make_mock_song("https://open.spotify.com/track/due")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    _set_backoff({
            backed_off.url: {"kind": "fail", "attempts": 1, "retry_after": future},
            due.url: {"kind": "fail", "attempts": 1, "retry_after": past},
        })
    simple_songs.return_value = [backed_off, due]
    mock_spotdl = _mock_spotdl([(due, None)], errors=[_download_error(due.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    assert result.attempted == 1  # the legacy entry without a reason still backs off
    data = _backoff()
    assert "reason" not in data[backed_off.url]  # untouched
    assert data[due.url]["attempts"] == 2
    assert data[due.url]["reason"] == "AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=abc"


# ---------------------------------------------------------------------------
# SpotifyPlaylists.songs (#178)
# ---------------------------------------------------------------------------


def _track(n: int) -> dict:
    return {
        "track": {
            "id": f"t{n}", "type": "track", "is_local": False, "name": f"Song {n}",
            "artists": [{"name": "Artist"}], "disc_number": 1, "track_number": n,
            "duration_ms": 200_000, "explicit": False,
            "external_urls": {"spotify": f"https://open.spotify.com/track/t{n}"},
            "external_ids": {"isrc": f"ISRC{n}"},
            "album": {"id": "a1", "name": "Album", "artists": [{"name": "Artist"}],
                      "album_type": "album", "release_date": "2001-01-01",
                      "total_tracks": 3, "images": []},
        }
    }


def _playlist_client() -> mock.MagicMock:
    """A SpotifyClient serving one page of three tracks; any per-track call fails the test."""
    client = mock.MagicMock()
    client.playlist.return_value = {
        "name": "keep", "owner": {"display_name": "me"}, "description": "",
        "images": [], "external_urls": {"spotify": "https://open.spotify.com/playlist/p"},
        "tracks": {"total": 3},
    }
    client.playlist_items.return_value = {"items": [_track(n) for n in (1, 2, 3)], "next": None}
    client.track.side_effect = AssertionError("per-track fetch")
    client.artist.side_effect = AssertionError("per-track fetch")
    client.album.side_effect = AssertionError("per-track fetch")
    return client


def test_playlist_songs_read_pages_only_never_per_track() -> None:
    """A per-track re-fetch (track/artist/album calls) got the app rate-limited for 17h."""
    from music_fetch.spotdl_ops import SpotifyPlaylists

    reader = SpotifyPlaylists.__new__(SpotifyPlaylists)
    with mock.patch("spotdl.types.playlist.SpotifyClient", return_value=_playlist_client()):
        songs = reader.songs("https://open.spotify.com/playlist/p")

    assert [s["name"] for s in songs] == ["Song 1", "Song 2", "Song 3"]
    assert {s["album_id"] for s in songs} == {"a1"}
    assert songs[0]["isrc"] == "ISRC1"
    assert songs[0]["tracks_count"] == 3


def test_sync_playlist_read_makes_no_per_track_calls(tmp_path: Path) -> None:
    """The real page reader: an unchanged playlist costs no track, artist or album calls (#183)."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    known = [{"url": f"https://open.spotify.com/track/t{n}", "name": f"Song {n}"} for n in (1, 2, 3)]
    mock_spotdl = mock.Mock()
    mock_spotdl.download_songs.return_value = []

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         mock.patch("spotdl.types.playlist.SpotifyClient", return_value=_playlist_client()):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=known)

    assert (result.attempted, result.removed_urls) == (0, set())
    mock_spotdl.download_songs.assert_not_called()
    mock_spotdl.search.assert_not_called()
    assert [s["url"] for s in result.songs] == [s["url"] for s in known]
    assert result.records == {s["url"]: s for s in known}


def test_download_song_downloads_from_the_entry_without_a_spotify_lookup(tmp_path) -> None:
    from music_fetch.spotdl_ops import download_song

    song = {"name": "Song", "url": "https://open.spotify.com/track/S"}
    spotdl_obj = mock.MagicMock()
    spotdl_obj.download_songs.return_value = [(mock.MagicMock(), tmp_path / "Song.m4a")]
    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl_obj) as make, \
         mock.patch("spotdl.types.song.Song.from_dict", return_value="SONG") as from_dict:
        assert download_song(song, tmp_path, tmp_path / "cookies.txt") == tmp_path / "Song.m4a"
    from_dict.assert_called_once_with(song)
    spotdl_obj.download_songs.assert_called_once_with(["SONG"])
    assert make.call_args.args[0]["output"] == str(tmp_path)


# ---------------------------------------------------------------------------
# Library check before download (#187)
# ---------------------------------------------------------------------------


def test_track_already_in_library_is_linked_not_downloaded(tmp_path: Path, caplog, simple_songs) -> None:
    """A new track the library holds gets a record without a download."""
    import logging
    output_dir, cookie_file = _setup_sync(tmp_path)
    known = _make_mock_song("https://open.spotify.com/track/known", title="Known")
    have = _make_mock_song("https://open.spotify.com/track/have", title="Elsewhere")
    new = _make_mock_song("https://open.spotify.com/track/new", title="New")
    simple_songs.return_value = [have, known, new]
    _set_backoff({have.url: {
        "kind": "miss", "attempts": 1, "retry_after": "2000-01-01T00:00:00+00:00",
    }})
    mock_spotdl = _mock_spotdl([(new, Path("/tmp/new.m4a"))])
    asked: list[str] = []

    def in_library(song: dict) -> bool:
        asked.append(song["url"])
        return song["url"] == have.url

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file, previous=[known.json],
            backoff=True, in_library=in_library,
        )

    assert asked == [have.url, new.url]  # known tracks aren't asked about
    mock_spotdl.download_songs.assert_called_once_with([new])
    assert (result.attempted, result.downloaded, result.linked) == (1, 1, 1)
    assert "[HAVE] Artist - Elsewhere" in caplog.text
    assert result.songs == [have.json, known.json, new.json]
    assert result.records == {have.url: have.json, known.url: known.json, new.url: new.json}
    assert have.url not in _backoff()


def test_linked_tracks_do_not_use_the_budget(tmp_path: Path, simple_songs) -> None:
    """The library check runs before the track budget, so a linked track frees its slot."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    have = _make_mock_song("https://open.spotify.com/track/have")
    new = _make_mock_song("https://open.spotify.com/track/new")
    simple_songs.return_value = [have, new]
    mock_spotdl = _mock_spotdl([(new, Path("/tmp/new.m4a"))])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file,
            track_limit=1, in_library=lambda song: song["url"] == have.url,
        )

    mock_spotdl.download_songs.assert_called_once_with([new])
    assert (result.attempted, result.linked) == (1, 1)


def test_rate_limit_inside_download_fails_the_sync_without_backoff(tmp_path: Path, simple_songs) -> None:
    """spotdl swallows a per-track Spotify error into (song, None); a long 429 must
    still fail the run, not back the tracks off as [FAIL] (#195)."""
    import music_fetch.spotify_limit as spotify_limit

    output_dir, cookie_file = _setup_sync(tmp_path)
    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}") for i in range(2)]
    simple_songs.return_value = songs
    mock_spotdl = mock.Mock()
    mock_spotdl.downloader.errors = []

    def _download(_batch):
        try:
            raise spotify_limit.trip(3600)
        except spotify_limit.SpotifyRateLimited:
            pass  # what spotdl's downloader does with it
        return [(s, None) for s in songs]

    mock_spotdl.download_songs.side_effect = _download

    with mock.patch.object(spotify_limit, "_limit", None), \
         mock.patch.object(spotify_limit, "_push"), \
         mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         pytest.raises(spotify_limit.SpotifyRateLimited):
        sync_playlist(PLAYLIST_URL, "mypl", [], lambda song: True, output_dir, cookie_file, backoff=True)

    assert _backoff() == {}


def test_download_fallback_applies_have_backoff_and_budget(tmp_path: Path) -> None:
    """Fallback tracks (#205) go through the same [HAVE], backoff and budget as a
    sync, into the playlist's inbox, without a store to write."""
    from music_fetch.spotdl_ops import download_fallback

    songs = [{"url": f"https://open.spotify.com/track/{i}", "name": f"T{i}", "artists": ["A"]} for i in range(4)]
    _set_backoff({songs[1]["url"]: {"kind": "fail", "attempts": 1, "retry_after": "2999-01-01T00:00:00+00:00"}})
    sent: list = []

    def fake_download(batch):
        sent.extend(batch)
        return [(s, tmp_path / "x.m4a") for s in batch]

    spotdl = mock.Mock()
    spotdl.downloader.errors = []
    spotdl.download_songs.side_effect = fake_download
    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl) as make, \
         mock.patch("spotdl.types.song.Song.from_dict", side_effect=lambda d: _make_mock_song(d["url"], d["name"])):
        result = download_fallback(
            songs, output_dir=tmp_path / "later", cookie_file=tmp_path / "c.txt", track_limit=1,
            backoff=True, in_library=lambda song: song["name"] == "T0",
        )

    assert [s.url for s in sent] == [songs[2]["url"]]  # T0 [HAVE], T1 [BACK], T3 [DEFER]
    assert (result.linked, result.attempted, result.downloaded) == (1, 1, 1)
    assert make.call_args[0][0]["output"].startswith(str(tmp_path / "later"))


# ---------------------------------------------------------------------------
# Output file names over 255 bytes (#225)
# ---------------------------------------------------------------------------

GLYPH = "⃝"  # COMBINING ENCLOSING CIRCLE: one character, three UTF-8 bytes


def _real_song(title: str, song_id: str = "4uLU6hMCjMI75M1A2tKUQC", artist: str = "Four Tet"):
    from spotdl.types.song import Song

    return Song.from_dict({
        "name": title, "artists": [artist], "artist": artist, "genres": [], "disc_number": 1,
        "disc_count": 1, "album_name": "album", "album_artist": artist, "duration": 200,
        "year": 2020, "date": "2020-01-01", "track_number": 1, "tracks_count": 8,
        "song_id": song_id, "explicit": False, "publisher": "", "isrc": None, "cover_url": None,
        "copyright_text": None, "url": f"https://open.spotify.com/track/{song_id}",
    })


def test_glyph_title_name_is_capped_in_bytes(tmp_path: Path) -> None:
    """200 glyph characters is under spotdl's 255-character cap but 600 bytes on disk;
    the name is cut on a character boundary and keeps the Spotify track ID."""
    from music_fetch.names import SPOTDL_NAME_BYTES
    from music_fetch.spotdl_ops import _capped_file_name

    out = _capped_file_name(_real_song(GLYPH * 200), str(tmp_path), "m4a")

    assert out.parent == tmp_path
    assert len(out.name.encode("utf-8")) <= SPOTDL_NAME_BYTES
    assert out.name.endswith(" [4uLU6hMCjMI75M1A2tKUQC].m4a")
    assert out.name.startswith("Four Tet - " + GLYPH)
    out.touch()  # the point of it all: the filesystem accepts the name
    assert out.exists()


def test_long_glyph_titles_get_distinct_names(tmp_path: Path) -> None:
    from music_fetch.spotdl_ops import _capped_file_name

    a = _capped_file_name(_real_song(GLYPH * 300, song_id="a" * 22), str(tmp_path), "m4a")
    b = _capped_file_name(_real_song(GLYPH * 300, song_id="b" * 22), str(tmp_path), "m4a")

    assert a != b


def test_ordinary_name_is_unchanged(tmp_path: Path) -> None:
    from music_fetch.spotdl_ops import _capped_file_name

    out = _capped_file_name(_real_song("Two Thousand and Seventeen"), str(tmp_path), "m4a",
                            restrict=None, file_name_length=None)

    assert out == tmp_path / "Four Tet - Two Thousand and Seventeen.m4a"


def test_name_cap_is_installed_on_the_downloader() -> None:
    from spotdl.download import downloader

    from music_fetch.spotdl_ops import _capped_file_name, _install_name_cap

    original = downloader.create_file_name
    try:
        _install_name_cap()
        _install_name_cap()
        assert downloader.create_file_name is _capped_file_name
    finally:
        downloader.create_file_name = original


# ---------------------------------------------------------------------------
# The want list (#228)
# ---------------------------------------------------------------------------


def test_want_list_drops_linked_and_backed_off_tracks_in_order() -> None:
    from datetime import datetime, timezone

    from music_fetch.spotdl_ops import want_list

    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}", title=f"T{i}") for i in range(4)]
    failures = {
        songs[1].url: {"kind": "fail", "retry_after": "2999-01-01T00:00:00+00:00"},
        songs[3].url: {"kind": "miss", "retry_after": "2000-01-01T00:00:00+00:00"},  # expired: due again
    }
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    wanted, linked, backed_off = want_list(songs, lambda song: song["name"] == "T0", failures, now)

    assert [s.url for s in wanted] == [songs[2].url, songs[3].url]
    assert linked == [songs[0]]
    assert backed_off == [songs[1]]
    assert want_list(songs, None, {}, now) == (songs, [], [])


def test_zero_budget_checks_the_playlist_and_defers_every_download(tmp_path: Path, simple_songs, caplog) -> None:
    """A spent budget still fetches, links [HAVE], applies backoff and records
    removals; it only defers the downloads, with no spotdl download call (#228)."""
    import logging

    output_dir, cookie_file = _setup_sync(tmp_path)
    gone = _make_mock_song("https://open.spotify.com/track/gone", title="Gone")
    have = _make_mock_song("https://open.spotify.com/track/have", title="Elsewhere")
    new = _make_mock_song("https://open.spotify.com/track/new", title="New")
    simple_songs.return_value = [have, new]
    mock_spotdl = _mock_spotdl([])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=mock_spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(
            output_dir=output_dir, cookie_file=cookie_file, previous=[gone.json],
            track_limit=0, backoff=True, in_library=lambda song: song["url"] == have.url,
        )

    mock_spotdl.download_songs.assert_not_called()
    assert (result.attempted, result.downloaded, result.linked) == (0, 0, 1)
    assert result.removed_urls == {gone.url}
    assert "[HAVE] Artist - Elsewhere" in caplog.text
    assert "Track budget: downloading 0 of 1 new track(s) this session (1 deferred to next run)" in caplog.text
    assert "[DEFER]" not in caplog.text
    assert result.songs == [have.json, new.json]
    assert result.records == {have.url: have.json, gone.url: gone.json}  # the deferred track has none yet


# ---------------------------------------------------------------------------
# Pinned source URLs (#247)
# ---------------------------------------------------------------------------


def test_load_pins_reads_ids_urls_and_labels(tmp_path: Path) -> None:
    from music_fetch.spotdl_ops import load_pins

    conf = tmp_path / "track-pins.conf"
    conf.write_text(
        "# a comment\n"
        "\n"
        "68ozhSrI4eLbnUgih1SlOT  https://music.youtube.com/watch?v=29Sh3yEaBP8&si=BQmU0Qvnv0Z64Jwc  track 1\n"
        "https://open.spotify.com/track/5sKY9vib0WClIKDr0qkaWH?si=x  https://youtu.be/aVqJbrkkdYk?t=3\n"
        "noUrlHere\n"
        "7rgHXYuIpmxO3bpXZ7ZNLR  ftp://example.com/x\n"
        "1XFW64AA5LnzFT510fKZSF  https://example.com/song.m4a\n"  # yt-dlp fetches YouTube only (homelab#2484)
        "2XFW64AA5LnzFT510fKZSF  https://music.youtube.com/playlist?list=OLAK5uy_l7iC3zWrsZ\n",
        encoding="utf-8",
    )

    assert load_pins(conf) == {
        "https://open.spotify.com/track/68ozhSrI4eLbnUgih1SlOT": "https://music.youtube.com/watch?v=29Sh3yEaBP8",
        "https://open.spotify.com/track/5sKY9vib0WClIKDr0qkaWH": "https://www.youtube.com/watch?v=aVqJbrkkdYk",
    }
    assert load_pins(tmp_path / "absent.conf") == {}


def test_want_list_puts_pinned_tracks_first_and_ignores_their_backoff() -> None:
    from datetime import datetime, timezone

    from music_fetch.spotdl_ops import want_list

    songs = [_make_mock_song(f"https://open.spotify.com/track/{i}", title=f"T{i}") for i in range(3)]
    failures = {songs[2].url: {"kind": "miss", "retry_after": "2999-01-01T00:00:00+00:00"}}
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    wanted, _, backed_off = want_list(songs, None, failures, now, pins={songs[2].url: "https://music.youtube.com/watch?v=x"})

    assert [s.url for s in wanted] == [songs[2].url, songs[0].url, songs[1].url]
    assert backed_off == []


def test_pinned_track_downloads_from_its_source_url(tmp_path: Path, caplog) -> None:
    """A pinned track skips spotdl's search (download_url set), ignores its
    backoff and gets the budget before the playlist's other tracks (#247)."""
    import logging

    from music_fetch.spotdl_ops import download_fallback

    songs = [{"url": f"https://open.spotify.com/track/{i}", "name": f"T{i}", "artists": ["A"]} for i in range(3)]
    _set_backoff({songs[2]["url"]: {"kind": "miss", "attempts": 2, "retry_after": "2999-01-01T00:00:00+00:00"}})
    pins_file = tmp_path / "track-pins.conf"
    pins_file.write_text("2  https://music.youtube.com/watch?v=pinnedVideo&si=share\n", encoding="utf-8")
    sent: list = []

    def fake_download(batch):
        sent.extend(batch)
        return [(s, tmp_path / "x.m4a") for s in batch]

    spotdl = mock.Mock()
    spotdl.downloader.errors = []
    spotdl.download_songs.side_effect = fake_download
    with caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"), \
         mock.patch("music_fetch.spotdl_ops.PINS_FILE", pins_file), \
         mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl), \
         mock.patch("spotdl.types.song.Song.from_dict", side_effect=lambda d: _make_mock_song(d["url"], d["name"])):
        result = download_fallback(
            songs, output_dir=tmp_path / "later", cookie_file=tmp_path / "c.txt", track_limit=1,
            backoff=True,
        )

    assert [s.url for s in sent] == [songs[2]["url"]]
    assert sent[0].download_url == "https://music.youtube.com/watch?v=pinnedVideo"
    assert "[PIN] Artist - T2 → downloading from https://music.youtube.com/watch?v=pinnedVideo" in caplog.text
    assert result.downloaded == 1
    assert songs[2]["url"] not in _backoff()


def test_pin_for_a_track_in_the_library_is_skipped(tmp_path: Path, caplog) -> None:
    import logging

    from music_fetch.spotdl_ops import _download_batch

    song = _make_mock_song("https://open.spotify.com/track/0", "T0")
    spotdl = mock.Mock()
    spotdl.downloader.errors = []
    with caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        batch = _download_batch(
            spotdl, [song], None, None, lambda song: True,
            pins={song.url: "https://music.youtube.com/watch?v=x"},
        )

    spotdl.download_songs.assert_not_called()
    assert (batch.attempted, len(batch.linked)) == (0, 1)
    assert "[PIN] Artist - T0 → already in the library; its pin is unused" in caplog.text




# ---------------------------------------------------------------------------
# sync_playlist — the store's want test (homelab#1914)
# ---------------------------------------------------------------------------


def test_wants_judges_a_known_track_by_its_previous_record(tmp_path: Path, simple_songs) -> None:
    """The page record lacks the ISRC the store's record carries; an item matched
    by that ISRC holds the track, so it is not downloaded again.  A new track is
    judged by its page record."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    known = _make_mock_song("https://open.spotify.com/track/K")
    new = _make_mock_song("https://open.spotify.com/track/N")
    previous = [{**known.json, "isrc": "GB1"}]
    simple_songs.return_value = [known, new]
    spotdl = _mock_spotdl([(new, Path("/tmp/n.m4a"))])
    judged: list[dict] = []

    def wants(song: dict) -> bool:
        judged.append(song)
        return song.get("isrc") != "GB1"

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=previous, wants=wants)

    assert judged == [previous[0], new.json]
    spotdl.download_songs.assert_called_once_with([new])
    assert (result.attempted, result.downloaded) == (1, 1)


def test_summary_counts_a_wanted_known_track_apart_from_a_new_one(
        tmp_path: Path, simple_songs, caplog) -> None:
    """A track the store held that it wants again (no library item holds it) is
    downloaded and counted as on the playlist before; one it holds and doesn't want
    is counted as held; neither gets its own INFO line (#320)."""
    import logging

    output_dir, cookie_file = _setup_sync(tmp_path)
    kept = _make_mock_song("https://open.spotify.com/track/K", title="Kept")
    lost = _make_mock_song("https://open.spotify.com/track/L", title="Lost")
    new = _make_mock_song("https://open.spotify.com/track/N", title="New")
    simple_songs.return_value = [kept, lost, new]
    spotdl = _mock_spotdl([(lost, Path("/tmp/l.m4a")), (new, Path("/tmp/n.m4a"))])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=[kept.json, lost.json],
                       wants=lambda song: song["url"] != kept.url)

    spotdl.download_songs.assert_called_once_with([lost, new])
    assert result.downloaded == 2
    assert "mypl: 3 track(s) on Spotify, 1 held or waiting, 2 wanted (1 on the playlist before, 1 new), 0 removed" in caplog.text
    assert "[SKIP]" not in caplog.text
    assert "[STORE-WANT]" not in caplog.text


def test_new_track_the_store_does_not_want_is_not_downloaded(tmp_path: Path, simple_songs, caplog) -> None:
    """A track new to the playlist that the library already holds (by the store's
    test) is [SKIP]: no library callback, no download and no record of its own."""
    import logging

    output_dir, cookie_file = _setup_sync(tmp_path)
    held = _make_mock_song("https://open.spotify.com/track/H", title="Held")
    new = _make_mock_song("https://open.spotify.com/track/N", title="New")
    simple_songs.return_value = [held, new]
    spotdl = _mock_spotdl([(new, Path("/tmp/n.m4a"))])
    asked: list[str] = []

    def in_library(song: dict) -> bool:
        asked.append(song["url"])
        return False

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl), \
         caplog.at_level(logging.INFO, logger="music_fetch.spotdl_ops"):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, in_library=in_library,
                       wants=lambda song: song["url"] != held.url)

    assert asked == [new.url]
    spotdl.download_songs.assert_called_once_with([new])
    assert (result.attempted, result.downloaded, result.linked) == (1, 1, 0)
    assert "mypl: 2 track(s) on Spotify, 1 held or waiting, 1 wanted (0 on the playlist before, 1 new), 0 removed" in caplog.text
    assert "[SKIP]" not in caplog.text
    assert result.songs == [held.json, new.json]
    assert result.records == {new.url: new.json}


def test_duplicate_urls_on_the_playlist_are_downloaded_once(tmp_path: Path, simple_songs) -> None:
    output_dir, cookie_file = _setup_sync(tmp_path)
    a = _make_mock_song("https://open.spotify.com/track/A", title="A")
    a_again = _make_mock_song("https://open.spotify.com/track/A", title="A")
    b = _make_mock_song("https://open.spotify.com/track/B", title="B")
    simple_songs.return_value = [a, a_again, b]
    spotdl = _mock_spotdl([(a, Path("/tmp/a.m4a")), (b, Path("/tmp/b.m4a"))])
    judged: list[str] = []

    def wants(song: dict) -> bool:
        judged.append(song["url"])
        return True

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, wants=wants)

    spotdl.download_songs.assert_called_once_with([a, b])
    assert judged == [a.url, b.url]
    assert (result.attempted, result.downloaded) == (2, 2)
    assert result.songs == [a.json, a_again.json, b.json]  # the playlist as Spotify lists it


def test_records_prefer_a_download_then_the_previous_record(tmp_path: Path, simple_songs) -> None:
    """A fresh download's record beats the store's; the store's beats the page
    record of a track the library holds ([HAVE])."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    redo = _make_mock_song("https://open.spotify.com/track/R", title="Redo")
    linked = _make_mock_song("https://open.spotify.com/track/L", title="Linked")
    previous = [{**redo.json, "genres": ["old"]}, {**linked.json, "isrc": "GB1"}]
    full_redo = _make_mock_song(redo.url, title="Redo")
    full_redo.json["genres"] = ["new"]
    simple_songs.return_value = [redo, linked]
    spotdl = _mock_spotdl([(full_redo, Path("/tmp/r.m4a"))])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=previous,
                       wants=lambda song: True, in_library=lambda song: song["url"] == linked.url)

    spotdl.download_songs.assert_called_once_with([redo])
    assert (result.downloaded, result.linked) == (1, 1)
    assert result.records == {redo.url: full_redo.json, linked.url: previous[1]}


def test_removed_urls_are_the_previous_songs_spotify_dropped(tmp_path: Path, simple_songs) -> None:
    output_dir, cookie_file = _setup_sync(tmp_path)
    url = "https://open.spotify.com/track/{}".format
    previous = [{"url": url(n), "name": n} for n in "ABC"] + [{"name": "a record without a URL"}]
    simple_songs.return_value = [_make_mock_song(url("A")), _make_mock_song(url("D"))]
    spotdl = _mock_spotdl([])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl):
        result = _sync(output_dir=output_dir, cookie_file=cookie_file, previous=previous, track_limit=0)

    assert result.removed_urls == {url("B"), url("C")}
    assert set(result.records) == {url("A"), url("B"), url("C")}  # D was deferred: no record


def test_sync_writes_no_file(tmp_path: Path, simple_songs) -> None:
    """The store is the caller's to write and the backoff is the database's: no .spotdl or other file appears."""
    output_dir, cookie_file = _setup_sync(tmp_path)
    ok, missing = (_make_mock_song(f"https://open.spotify.com/track/{n}") for n in ("ok", "miss"))
    simple_songs.return_value = [ok, missing]
    spotdl = _mock_spotdl([(ok, Path("/tmp/ok.m4a")), (missing, None)], errors=[_lookup_error(missing.url)])

    with mock.patch("music_fetch.spotdl_ops._make_spotdl", return_value=spotdl):
        _sync(output_dir=output_dir, cookie_file=cookie_file, backoff=True)

    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == [Path("mypl")]
    assert missing.url in _backoff()

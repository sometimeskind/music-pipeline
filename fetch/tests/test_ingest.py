"""Tests for pipeline.ingest — pure-Python logic (no I/O, no external tools)."""

import json
from pathlib import Path

import pytest

from music_fetch.ingest import classify_failure, _collect_removals, _deadline_reached, reconcile_playlists, PendingRemovals, RemovedTrack
from music_fetch.spotdl_ops import SyncResult, find_track_in_snapshot


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg, expected",
    [
        ("SpotifyError: invalid credentials", "auth_spotify"),
        ("Invalid Credentials provided", "auth_spotify"),
        ("HTTP Error 403: Forbidden", "auth_youtube"),
        ("Sign in to confirm your age", "auth_youtube"),
        ("cookies are required", "auth_youtube"),
        ("429 Too Many Requests", "rate_limited"),
        ("too many requests, back off", "rate_limited"),
        ("some unexpected download error", "spotdl_error"),
        ("", "spotdl_error"),
    ],
)
def test_classify_failure(msg: str, expected: str) -> None:
    assert classify_failure(msg) == expected


# ---------------------------------------------------------------------------
# _deadline_reached
# ---------------------------------------------------------------------------


def test_deadline_reached_no_timeout() -> None:
    assert _deadline_reached(0.0, None) is False


def test_deadline_reached_not_yet() -> None:
    assert _deadline_reached(50.0, 100) is False


def test_deadline_reached_exactly_at_limit() -> None:
    assert _deadline_reached(100.0, 100) is True


def test_deadline_reached_past_limit() -> None:
    assert _deadline_reached(200.0, 100) is True


# ---------------------------------------------------------------------------
# find_track_in_snapshot
# ---------------------------------------------------------------------------

SNAPSHOT = [
    {"url": "https://spotify.com/track/A", "name": "Song A", "artists": ["Artist 1"]},
    {"url": "https://spotify.com/track/B", "name": "Song B", "artists": ["Artist 2"]},
]


def test_find_track_found() -> None:
    result = find_track_in_snapshot(SNAPSHOT, "https://spotify.com/track/A")
    assert result is not None
    assert result["name"] == "Song A"


def test_find_track_not_found() -> None:
    result = find_track_in_snapshot(SNAPSHOT, "https://spotify.com/track/Z")
    assert result is None


def test_find_track_empty_snapshot() -> None:
    assert find_track_in_snapshot([], "https://spotify.com/track/A") is None


# ---------------------------------------------------------------------------
# Snapshot diff logic (as it would be used in ingest.run)
# ---------------------------------------------------------------------------

def _diff(old: list[dict], new_urls: set[str]) -> set[str]:
    """Replicate the removal-detection logic from ingest.run."""
    old_urls = {s["url"] for s in old}
    return old_urls - new_urls


def test_diff_detects_removal() -> None:
    old = [
        {"url": "https://spotify.com/track/A"},
        {"url": "https://spotify.com/track/B"},
    ]
    new_urls = {"https://spotify.com/track/A"}
    removed = _diff(old, new_urls)
    assert removed == {"https://spotify.com/track/B"}


def test_diff_no_removal() -> None:
    old = [{"url": "https://spotify.com/track/A"}]
    new_urls = {"https://spotify.com/track/A", "https://spotify.com/track/B"}
    assert _diff(old, new_urls) == set()


def test_diff_all_removed() -> None:
    old = [{"url": "https://spotify.com/track/A"}, {"url": "https://spotify.com/track/B"}]
    assert _diff(old, set()) == {"https://spotify.com/track/A", "https://spotify.com/track/B"}


def test_diff_empty_old() -> None:
    assert _diff([], {"https://spotify.com/track/A"}) == set()


# ---------------------------------------------------------------------------
# Track budget logic (as it would be used in spotdl_ops.sync_playlist)
# ---------------------------------------------------------------------------

def _apply_budget(
    old_urls: set[str],
    all_new_urls: list[str],
    budget: int | None,
) -> tuple[list[str], list[str]]:
    """Replicate the track-limiting logic from spotdl_ops.sync_playlist.

    Returns (batch_downloaded, songs_written_to_spotdl_file).
    """
    truly_new = [u for u in all_new_urls if u not in old_urls]
    batch = truly_new if budget is None else truly_new[:budget]
    batch_set = set(batch)
    written = [u for u in all_new_urls if u in old_urls or u in batch_set]
    return batch, written


def test_budget_none_downloads_all_new() -> None:
    old = {"https://spotify.com/track/A"}
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B", "https://spotify.com/track/C"]
    batch, written = _apply_budget(old, new, budget=None)
    assert set(batch) == {"https://spotify.com/track/B", "https://spotify.com/track/C"}
    assert set(written) == set(new)


def test_budget_limits_downloads_and_defers_remainder() -> None:
    old: set[str] = set()
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B", "https://spotify.com/track/C"]
    batch, written = _apply_budget(old, new, budget=2)
    assert batch == ["https://spotify.com/track/A", "https://spotify.com/track/B"]
    # C is deferred — not written to the snapshot
    assert "https://spotify.com/track/C" not in written
    assert set(written) == {"https://spotify.com/track/A", "https://spotify.com/track/B"}


def test_budget_gte_new_behaves_like_unlimited() -> None:
    old: set[str] = set()
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B"]
    batch_limited, written_limited = _apply_budget(old, new, budget=10)
    batch_unlimited, written_unlimited = _apply_budget(old, new, budget=None)
    assert batch_limited == batch_unlimited
    assert written_limited == written_unlimited


def test_budget_deferred_tracks_reappear_next_session() -> None:
    """Tracks excluded from the snapshot show up as new on the next run."""
    old: set[str] = set()
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B", "https://spotify.com/track/C"]

    # Session 1: budget=1, downloads A
    batch1, written1 = _apply_budget(old, new, budget=1)
    assert batch1 == ["https://spotify.com/track/A"]

    # Session 2: old_urls = what was written to .spotdl after session 1
    old2 = set(written1)
    batch2, written2 = _apply_budget(old2, new, budget=1)
    assert batch2 == ["https://spotify.com/track/B"]

    # Session 3
    old3 = set(written2)
    batch3, _ = _apply_budget(old3, new, budget=1)
    assert batch3 == ["https://spotify.com/track/C"]


def test_budget_spans_playlists() -> None:
    """Simulate budget consumption across two playlists."""
    budget = 5

    # Playlist A: 3 new tracks
    old_a: set[str] = set()
    new_a = [f"https://spotify.com/track/A{i}" for i in range(3)]
    batch_a, _ = _apply_budget(old_a, new_a, budget=budget)
    budget -= len(batch_a)  # budget → 2

    # Playlist B: 4 new tracks, only 2 slots remain
    old_b: set[str] = set()
    new_b = [f"https://spotify.com/track/B{i}" for i in range(4)]
    batch_b, written_b = _apply_budget(old_b, new_b, budget=budget)

    assert len(batch_a) == 3
    assert len(batch_b) == 2  # capped by remaining budget
    # B2 and B3 are deferred
    assert "https://spotify.com/track/B2" not in written_b
    assert "https://spotify.com/track/B3" not in written_b


# ---------------------------------------------------------------------------
# sync_playlists — config ordering
# ---------------------------------------------------------------------------


def test_sync_playlists_processes_in_config_order(tmp_path: Path) -> None:
    """Playlists are synced in playlists.conf order, not alphabetically.

    Regression: previously sorted(glob("*.spotdl")) produced alphabetical
    order, so 'later' was processed before 'wedding' even though 'wedding'
    was listed first in the config.
    """
    import unittest.mock as mock
    from music_fetch import ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()

    # Config order: aaaaaaah, wedding, later
    conf = tmp_path / "playlists.conf"
    conf.write_text(
        "aaaaaaah  https://open.spotify.com/playlist/A\n"
        "wedding   https://open.spotify.com/playlist/W\n"
        "later     https://open.spotify.com/playlist/L\n",
        encoding="utf-8",
    )

    # Alphabetical order would be: aaaaaaah, later, wedding
    for name in ("aaaaaaah", "later", "wedding"):
        (spotdl_dir / f"{name}.spotdl").write_text(
            '{"type":"sync","query":["https://open.spotify.com/playlist/X"],"songs":[]}',
            encoding="utf-8",
        )

    processed: list[str] = []

    def fake_sync(spotdl_file, **_kwargs):
        processed.append(spotdl_file.stem)
        return SyncResult(set(), 0, 0, 0, 0, {})

    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync):
        ingest.sync_playlists([], IngestMetrics())

    assert processed == ["aaaaaaah", "wedding", "later"]


# ---------------------------------------------------------------------------
# _collect_removals
# ---------------------------------------------------------------------------

SNAPSHOT = [
    {"url": "https://spotify.com/track/A", "name": "Song A", "artists": ["Artist 1"]},
    {"url": "https://spotify.com/track/B", "name": "Song B", "artists": []},
]


def test_collect_removals_normal_case() -> None:
    pending: list[RemovedTrack] = []
    _collect_removals(pending, {"https://spotify.com/track/A"}, SNAPSHOT, "my-playlist")
    assert pending == [RemovedTrack(title="Song A", artist="Artist 1", source="my-playlist")]


def test_collect_removals_url_not_in_snapshot() -> None:
    """URL missing from snapshot → entry is skipped, no crash."""
    pending: list[RemovedTrack] = []
    _collect_removals(pending, {"https://spotify.com/track/Z"}, SNAPSHOT, "my-playlist")
    assert pending == []


def test_collect_removals_empty_artists() -> None:
    """artists=[] → artist field defaults to empty string."""
    pending: list[RemovedTrack] = []
    _collect_removals(pending, {"https://spotify.com/track/B"}, SNAPSHOT, "my-playlist")
    assert pending == [RemovedTrack(title="Song B", artist="", source="my-playlist")]


def test_collect_removals_no_removed_urls() -> None:
    pending: list[RemovedTrack] = []
    _collect_removals(pending, set(), SNAPSHOT, "my-playlist")
    assert pending == []


# ---------------------------------------------------------------------------
# run() — PendingRemovals return type
# ---------------------------------------------------------------------------


def test_run_returns_pending_removals_empty(tmp_path: Path) -> None:
    """run() returns PendingRemovals(tracks=[], remove_sources=[]) when nothing to remove."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()

    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.object(ingest, "CONF_PATH", tmp_path / "missing.conf"), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
         mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024**3)), \
         mock.patch("music_fetch.ingest.IngestMetrics"), \
         mock.patch("music_fetch.ingest._jitter"):
        result = ingest.run()

    assert isinstance(result, PendingRemovals)
    assert result.tracks == []
    assert result.remove_sources == []


def test_run_returns_pending_removals_with_remove_sources(tmp_path: Path) -> None:
    """run() returns PendingRemovals with remove_sources from reconcile_playlists."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()

    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.object(ingest, "CONF_PATH", tmp_path / "missing.conf"), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
         mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024**3)), \
         mock.patch("music_fetch.ingest.reconcile_playlists", return_value=["gone-playlist"]), \
         mock.patch("music_fetch.ingest.IngestMetrics"), \
         mock.patch("music_fetch.ingest._jitter"):
        result = ingest.run()

    assert isinstance(result, PendingRemovals)
    assert result.remove_sources == ["gone-playlist"]
    assert result.tracks == []


def test_run_reconcile_failure_sets_reconcile_error_reason(tmp_path: Path) -> None:
    """A reconcile_playlists failure sets failure_reason='reconcile_error', not 'unexpected_error'."""
    import unittest.mock as mock
    from music_fetch import ingest

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()

    captured: list = []

    class FakeMetrics:
        def __init__(self):
            self.success = True
            self.failure_reason = ""
            self.tracks_attempted = 0
            self.tracks_downloaded = 0
            self.tracks_missed = 0
            self.tracks_failed = 0
            self.playlists_total = 0
            self.playlists_skipped = 0
            self.playlists_deferred = 0
            self.cookies_expired = False
            self.duration_seconds = 0
            captured.append(self)

        def push(self):
            pass

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
         mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024**3)), \
         mock.patch("music_fetch.ingest.reconcile_playlists", side_effect=RuntimeError("conf error")), \
         mock.patch("music_fetch.ingest.IngestMetrics", FakeMetrics), \
         mock.patch("music_fetch.ingest._jitter"):
        with pytest.raises(RuntimeError):
            ingest.run()

    assert len(captured) == 1
    assert captured[0].failure_reason == "reconcile_error"


# ---------------------------------------------------------------------------
# reconcile_playlists
# ---------------------------------------------------------------------------


def _make_conf(tmp_path: Path, lines: list[str]) -> Path:
    conf = tmp_path / "playlists.conf"
    conf.write_text("\n".join(lines), encoding="utf-8")
    return conf


def test_reconcile_no_conf(tmp_path: Path) -> None:
    """Returns empty list if playlists.conf does not exist."""
    import unittest.mock as mock
    from music_fetch import ingest

    missing = tmp_path / "playlists.conf"
    with mock.patch.object(ingest, "CONF_PATH", missing):
        result = reconcile_playlists()

    assert result == []


def test_reconcile_provisions_new_playlist(tmp_path: Path) -> None:
    """New playlist entry → save_playlist is called; .spotdl file is created."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = _make_conf(tmp_path, ["my-playlist  https://open.spotify.com/playlist/abc"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch("music_fetch.ingest.save_playlist") as mock_save:
        result = reconcile_playlists()

    mock_save.assert_called_once()
    call_kwargs = mock_save.call_args[1]
    assert call_kwargs["url"] == "https://open.spotify.com/playlist/abc"
    assert call_kwargs["spotdl_file"] == spotdl_dir / "my-playlist.spotdl"
    assert result == []


def test_reconcile_skips_existing_spotdl(tmp_path: Path) -> None:
    """Playlist whose .spotdl already exists is not re-provisioned."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "my-playlist.spotdl").write_text("{}", encoding="utf-8")
    conf = _make_conf(tmp_path, ["my-playlist  https://open.spotify.com/playlist/abc"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_fetch.ingest.save_playlist") as mock_save:
        result = reconcile_playlists()

    mock_save.assert_not_called()
    assert result == []


def test_reconcile_creates_nosync_sentinel(tmp_path: Path) -> None:
    """Playlist marked nosync in config → sentinel file is created."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "frozen.spotdl").write_text("{}", encoding="utf-8")
    conf = _make_conf(tmp_path, ["frozen  https://open.spotify.com/playlist/abc  nosync"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_fetch.ingest.save_playlist"):
        reconcile_playlists()

    assert (spotdl_dir / "frozen.nosync").exists()


def test_reconcile_removes_nosync_sentinel(tmp_path: Path) -> None:
    """Playlist with nosync removed from config → sentinel file is deleted."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "mypl.spotdl").write_text("{}", encoding="utf-8")
    (spotdl_dir / "mypl.nosync").touch()
    conf = _make_conf(tmp_path, ["mypl  https://open.spotify.com/playlist/abc"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_fetch.ingest.save_playlist"):
        reconcile_playlists()

    assert not (spotdl_dir / "mypl.nosync").exists()


def test_reconcile_detects_removed_playlist(tmp_path: Path) -> None:
    """Playlist on disk but absent from config → queued for removal, files deleted."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    # Playlist on disk but not in config
    (spotdl_dir / "gone.spotdl").write_text("{}", encoding="utf-8")
    (spotdl_dir / "gone").mkdir()
    (spotdl_dir / "gone" / "track.m4a").touch()
    # Playlist in both config and disk
    (spotdl_dir / "kept.spotdl").write_text("{}", encoding="utf-8")
    conf = _make_conf(tmp_path, ["kept  https://open.spotify.com/playlist/abc"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_fetch.ingest.save_playlist"):
        result = reconcile_playlists()

    assert result == ["gone"]
    assert not (spotdl_dir / "gone.spotdl").exists()
    assert not (spotdl_dir / "gone").exists()
    assert (spotdl_dir / "kept.spotdl").exists()


# ---------------------------------------------------------------------------
# _preflight
# ---------------------------------------------------------------------------


def test_preflight_missing_cookies(tmp_path: Path) -> None:
    import unittest.mock as mock
    from music_fetch import ingest

    with mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
        result = ingest.preflight()

    assert result == "missing_cookies"


def test_preflight_missing_spotify_env(tmp_path: Path) -> None:
    import unittest.mock as mock
    from music_fetch import ingest

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.dict("os.environ", {}, clear=True):
        result = ingest.preflight()

    assert result == "auth_spotify"


def test_preflight_disk_full(tmp_path: Path) -> None:
    import shutil
    import unittest.mock as mock
    from music_fetch import ingest

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()

    fake_usage = shutil.disk_usage.__class__  # just need a namedtuple-like; use mock
    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
         mock.patch("shutil.disk_usage", return_value=mock.Mock(free=512 * 1024 * 1024)):  # 0.5 GB
        result = ingest.preflight()

    assert result == "disk_full"


def test_preflight_ok(tmp_path: Path) -> None:
    import unittest.mock as mock
    from music_fetch import ingest

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
         mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024**3)):  # 10 GB
        result = ingest.preflight()

    assert result is None


def test_reconcile_deletes_nosync_for_removed_playlist(tmp_path: Path) -> None:
    """Removed playlist's .nosync sentinel is also deleted."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "gone.spotdl").write_text("{}", encoding="utf-8")
    (spotdl_dir / "gone.nosync").touch()
    conf = _make_conf(tmp_path, [])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_fetch.ingest.save_playlist"):
        result = reconcile_playlists()

    assert result == ["gone"]
    assert not (spotdl_dir / "gone.nosync").exists()


# ---------------------------------------------------------------------------
# sync_playlists — cookie-expiry detection from per-track failures (issue #159)
# ---------------------------------------------------------------------------

_403_REASON = (
    "AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=abc "
    "(caused by DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden; "
    "HTTPError: HTTP Error 403: Forbidden)"
)
_OTHER_REASON = "AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=abc"


def _run_sync_playlists(tmp_path: Path, results: list, cookie_file_exists: bool = True):
    """Run ingest.sync_playlists over len(results) playlists with sync_playlist stubbed out.

    Returns (metrics, caplog-free) — callers that need log output wrap in caplog themselves.
    """
    import unittest.mock as mock
    from music_fetch import ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir(exist_ok=True)
    for i in range(len(results)):
        (spotdl_dir / f"pl{i}.spotdl").write_text(
            '{"type":"sync","query":["https://open.spotify.com/playlist/X"],"songs":[]}',
            encoding="utf-8",
        )
    cookie_file = tmp_path / "cookies.txt"
    if cookie_file_exists:
        cookie_file.touch()

    queued = iter(results)
    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", tmp_path / "missing.conf"), \
         mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=lambda **_kwargs: next(queued)), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists([], metrics)
    return metrics


def test_cookies_expired_set_from_403_track_failure(tmp_path: Path, caplog) -> None:
    """A single [FAIL] whose reason classifies as auth_youtube flags cookies as expired (issue #159).

    The sync itself succeeds (other tracks downloaded), so last_run_success stays 1.
    """
    import logging
    from music_fetch.spotdl_ops import SyncResult

    with caplog.at_level(logging.WARNING, logger="music_fetch.ingest"):
        metrics = _run_sync_playlists(tmp_path, [
            SyncResult(set(), 3, 2, 0, 1, {"https://open.spotify.com/track/1": _403_REASON}),
        ])

    assert metrics.cookies_expired is True
    assert metrics.success is True
    assert metrics.failure_reason == ""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "cookies" in r.getMessage().lower()]
    assert len(warnings) == 1
    assert "HTTP Error 403: Forbidden" in warnings[0].getMessage()
    assert "https://open.spotify.com/track/1" in warnings[0].getMessage()


def test_cookies_expired_warning_logged_once_across_playlists(tmp_path: Path, caplog) -> None:
    """Many 403s across playlists produce one WARNING, not one per track."""
    import logging
    from music_fetch.spotdl_ops import SyncResult

    with caplog.at_level(logging.WARNING, logger="music_fetch.ingest"):
        metrics = _run_sync_playlists(tmp_path, [
            SyncResult(set(), 2, 1, 0, 1, {"https://open.spotify.com/track/1": _403_REASON}),
            SyncResult(set(), 2, 1, 0, 1, {"https://open.spotify.com/track/2": _403_REASON}),
        ])

    assert metrics.cookies_expired is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "cookies" in r.getMessage().lower()]
    assert len(warnings) == 1


def test_cookies_expired_not_set_without_cookie_file(tmp_path: Path) -> None:
    """A 403 without a cookie file configured is not evidence of expired cookies."""
    from music_fetch.spotdl_ops import SyncResult

    metrics = _run_sync_playlists(
        tmp_path,
        [SyncResult(set(), 3, 2, 0, 1, {"https://open.spotify.com/track/1": _403_REASON})],
        cookie_file_exists=False,
    )

    assert metrics.cookies_expired is False
    assert metrics.success is True


def test_cookies_expired_not_set_for_non_auth_failure(tmp_path: Path) -> None:
    """A [FAIL] that doesn't classify as auth_youtube (and isn't a total wipeout) leaves the gauge alone."""
    from music_fetch.spotdl_ops import SyncResult

    metrics = _run_sync_playlists(tmp_path, [
        SyncResult(set(), 3, 2, 0, 1, {"https://open.spotify.com/track/1": _OTHER_REASON}),
    ])

    assert metrics.cookies_expired is False


def test_cookies_expired_heuristic_all_attempted_failed(tmp_path: Path, caplog) -> None:
    """Safety net: attempted > 0, downloaded == 0, failed == attempted flags cookies even without a 403."""
    import logging
    from music_fetch.spotdl_ops import SyncResult

    with caplog.at_level(logging.WARNING, logger="music_fetch.ingest"):
        metrics = _run_sync_playlists(tmp_path, [
            SyncResult(set(), 20, 0, 0, 20, {f"https://open.spotify.com/track/{i}": _OTHER_REASON for i in range(20)}),
            SyncResult(set(), 5, 0, 0, 5, {f"https://open.spotify.com/track/b{i}": _OTHER_REASON for i in range(5)}),
        ])

    assert (metrics.tracks_attempted, metrics.tracks_downloaded, metrics.tracks_failed) == (25, 0, 25)
    assert metrics.cookies_expired is True
    assert metrics.success is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("25" in w and "cookies" in w.lower() for w in warnings), warnings


def test_cookies_expired_heuristic_ignores_miss_only_runs(tmp_path: Path) -> None:
    """[MISS] tracks (no YouTube source) are not download failures; the heuristic must not fire."""
    from music_fetch.spotdl_ops import SyncResult

    metrics = _run_sync_playlists(tmp_path, [SyncResult(set(), 4, 0, 4, 0, {})])

    assert (metrics.tracks_attempted, metrics.tracks_downloaded, metrics.tracks_missed, metrics.tracks_failed) == (4, 0, 4, 0)
    assert metrics.cookies_expired is False


def test_cookies_expired_heuristic_ignores_mixed_miss_and_fail(tmp_path: Path) -> None:
    """downloaded == 0 but failed < attempted (some [MISS]) is not a wipeout."""
    from music_fetch.spotdl_ops import SyncResult

    metrics = _run_sync_playlists(tmp_path, [
        SyncResult(set(), 4, 0, 1, 3, {f"https://open.spotify.com/track/{i}": _OTHER_REASON for i in range(3)}),
    ])

    assert metrics.cookies_expired is False


def test_cookies_expired_heuristic_ignores_empty_run(tmp_path: Path) -> None:
    """Nothing attempted (everything already synced) is not a wipeout."""
    from music_fetch.spotdl_ops import SyncResult

    metrics = _run_sync_playlists(tmp_path, [SyncResult(set(), 0, 0, 0, 0, {})])

    assert metrics.cookies_expired is False

"""Tests for pipeline.ingest — pure-Python logic (no I/O, no external tools)."""

from pathlib import Path

import pytest

from music_fetch import ingest, store
from music_fetch.ingest import classify_failure, _deadline_reached, reconcile_playlists
from music_fetch.spotdl_ops import SyncResult


@pytest.fixture(autouse=True)
def _state_dirs(tmp_path: Path, monkeypatch):
    """Playlist stores and .m3u files land in tmp_path, never under /root (homelab#1914)."""
    monkeypatch.setattr(ingest, "PLAYLISTS_DIR", tmp_path / "playlists")


def _seed_albums(records: dict) -> None:
    """Write *records* (key → record) to the music database as the album store."""
    from music_fetch import albums, state

    rows = albums.State(albums={key: {**state.new_record(key), **record} for key, record in records.items()})
    rows.save()


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
        ("Spotify rate-limited: Retry-After 62939s, until 2026-10-05 03:00:00 UTC", "spotify_rate_limited"),
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
# sync_playlists — config ordering
# ---------------------------------------------------------------------------


def test_sync_playlists_processes_in_config_order(tmp_path: Path) -> None:
    """Playlists are synced in playlists.conf order, not alphabetically.

    Regression: an earlier sorted(glob("*.spotdl")) produced alphabetical
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
        "aaaaaaah  https://open.spotify.com/playlist/A000000000000000000000\n"
        "wedding   https://open.spotify.com/playlist/W000000000000000000000\n"
        "later     https://open.spotify.com/playlist/L000000000000000000000\n",
        encoding="utf-8",
    )

    # Alphabetical order would be: aaaaaaah, later, wedding
    processed: list[str] = []

    def fake_sync(name, **_kwargs):
        processed.append(name)
        return SyncResult(set(), 0, 0, 0, 0, {})

    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(IngestMetrics(), resolves=lambda song: False)

    assert processed == ["aaaaaaah", "wedding", "later"]


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


def test_run_reconciles_then_syncs_with_the_scan_hooks(tmp_path: Path) -> None:
    """run() reconciles playlists.conf, then syncs with music_scan's view of the library."""
    import unittest.mock as mock
    from contextlib import contextmanager

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.touch()
    hooks = {"resolves": lambda song: False, "held": frozenset({"https://open.spotify.com/track/H"}),
             "in_library": lambda pl, song: False}
    order: list[str] = []

    @contextmanager
    def fake_hooks():
        order.append("hooks")
        yield hooks

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch.dict("os.environ", {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
         mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 * 1024**3)), \
         mock.patch("music_scan.scan.sync_hooks", fake_hooks), \
         mock.patch("music_fetch.ingest.reconcile_playlists",
                    side_effect=lambda: order.append("reconcile") or ["gone"]), \
         mock.patch("music_fetch.ingest.sync_playlists",
                    side_effect=lambda *a, **kw: order.append("sync")) as sync, \
         mock.patch("music_fetch.ingest.IngestMetrics"), \
         mock.patch("music_fetch.ingest._jitter"):
        assert ingest.run() is None

    assert order == ["reconcile", "hooks", "sync"]
    kwargs = sync.call_args.kwargs
    assert {k: kwargs[k] for k in hooks} == hooks
    assert isinstance(kwargs["start"], float)


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
            self.tracks_linked = 0
            self.playlists_total = 0
            self.playlists_skipped = 0
            self.playlists_deferred = 0
            self.cookies_expired = False
            self.duration_seconds = 0
            captured.append(self)

        def push(self):
            pass

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
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
    """A new playlist gets its download dir, and no .spotdl stub: its first sync writes its store."""
    import unittest.mock as mock

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = _make_conf(tmp_path, ["my-playlist  https://open.spotify.com/playlist/abc0000000000000000000"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"):
        result = reconcile_playlists()

    assert result == []
    assert (spotdl_dir / "my-playlist").is_dir()
    assert not list(tmp_path.rglob("*.spotdl"))
    assert store.names() == []


def test_reconcile_leaves_an_existing_playlist_alone(tmp_path: Path) -> None:
    import unittest.mock as mock

    spotdl_dir = tmp_path / "spotdl"
    (spotdl_dir / "my-playlist").mkdir(parents=True)
    (spotdl_dir / "my-playlist" / "track.m4a").touch()
    store.save(store.Store.from_songs("my-playlist", [{"url": "u"}]))
    conf = _make_conf(tmp_path, ["my-playlist  https://open.spotify.com/playlist/abc0000000000000000000"])

    with mock.patch.object(ingest, "CONF_PATH", conf), mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        assert reconcile_playlists() == []

    assert (spotdl_dir / "my-playlist" / "track.m4a").exists()
    assert store.load("my-playlist").songs == [{"url": "u"}]


def test_reconcile_creates_nosync_sentinel(tmp_path: Path) -> None:
    """Playlist marked nosync in config → sentinel file is created."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = _make_conf(tmp_path, ["frozen  https://open.spotify.com/playlist/abc0000000000000000000  nosync"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        reconcile_playlists()

    assert (spotdl_dir / "frozen.nosync").exists()


def test_reconcile_removes_nosync_sentinel(tmp_path: Path) -> None:
    """Playlist with nosync removed from config → sentinel file is deleted."""
    import unittest.mock as mock
    from music_fetch import ingest

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "mypl.nosync").touch()
    conf = _make_conf(tmp_path, ["mypl  https://open.spotify.com/playlist/abc0000000000000000000"])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        reconcile_playlists()

    assert not (spotdl_dir / "mypl.nosync").exists()


def test_reconcile_detects_removed_playlist(tmp_path: Path) -> None:
    """A playlist with a store but absent from config loses its store, sentinels, dir and .m3u."""
    import unittest.mock as mock

    spotdl_dir = tmp_path / "spotdl"
    playlists_dir = tmp_path / "playlists"
    (spotdl_dir / "gone").mkdir(parents=True)
    (spotdl_dir / "gone" / "track.m4a").touch()
    (spotdl_dir / "gone.nosync").touch()
    (spotdl_dir / "gone.album").touch()
    (spotdl_dir / "kept").mkdir()
    playlists_dir.mkdir()
    for name in ("gone", "kept"):
        store.save(store.Store.from_songs(name, [{"url": f"u-{name}"}]))
        (playlists_dir / f"{name}.m3u").write_text("/library/x.m4a\n", encoding="utf-8")
    conf = _make_conf(tmp_path, ["kept  https://open.spotify.com/playlist/abc0000000000000000000"])

    with mock.patch.object(ingest, "CONF_PATH", conf), mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        result = reconcile_playlists()

    assert result == ["gone"]
    assert store.names() == ["kept"]
    assert not (spotdl_dir / "gone").exists()
    assert not (spotdl_dir / "gone.nosync").exists()
    assert not (spotdl_dir / "gone.album").exists()
    assert not (playlists_dir / "gone.m3u").exists()
    assert (playlists_dir / "kept.m3u").exists()
    assert (spotdl_dir / "kept").is_dir()


def test_reconcile_leaves_later_m3u_to_the_later_queue(tmp_path: Path) -> None:
    """homelab#2478: once the state is in the database the Later queue writes later.m3u, so the later
    playlist leaving playlists.conf doesn't delete it; another playlist's .m3u still goes."""
    import unittest.mock as mock

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    for name in ("later", "keep"):
        (playlists_dir / f"{name}.m3u").write_text("/library/x.m4a\n", encoding="utf-8")
    conf = _make_conf(tmp_path, ["kept  https://open.spotify.com/playlist/abc0000000000000000000"])
    with mock.patch.object(ingest, "CONF_PATH", conf), mock.patch.object(ingest, "SPOTDL_DIR", tmp_path / "spotdl"), \
         mock.patch.object(store, "names", return_value=["keep", "later"]), mock.patch.object(store, "remove"):
        assert reconcile_playlists() == ["keep", "later"]
    assert (playlists_dir / "later.m3u").exists()
    assert not (playlists_dir / "keep.m3u").exists()


def test_reconcile_ignores_a_leftover_spotdl_file(tmp_path: Path) -> None:
    """A .spotdl file is never read: without a store, its playlist is not 'removed' (homelab#1914)."""
    import unittest.mock as mock

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "old.spotdl").write_text("{}", encoding="utf-8")
    conf = _make_conf(tmp_path, [])

    with mock.patch.object(ingest, "CONF_PATH", conf), mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        assert reconcile_playlists() == []

    assert (spotdl_dir / "old.spotdl").read_text(encoding="utf-8") == "{}"


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
    store.save(store.Store.from_songs("gone", []))
    (spotdl_dir / "gone.nosync").touch()
    conf = _make_conf(tmp_path, [])

    with mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        result = reconcile_playlists()

    assert result == ["gone"]
    assert not (spotdl_dir / "gone.nosync").exists()
    assert store.names() == []


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
    conf = _make_conf(tmp_path, [f"pl{i}  https://open.spotify.com/playlist/X{i:021}" for i in range(len(results))])
    cookie_file = tmp_path / "cookies.txt"
    if cookie_file_exists:
        cookie_file.touch()

    queued = iter(results)
    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", cookie_file), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=lambda **_kwargs: next(queued)), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(metrics, resolves=lambda song: False)
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


def test_reconcile_album_sentinel_follows_config(tmp_path: Path) -> None:
    """The album flag creates a .album sentinel; dropping the flag removes it."""
    import unittest.mock as mock
    from music_fetch import ingest

    conf = tmp_path / "playlists.conf"
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    with mock.patch.object(ingest, "CONF_PATH", conf), mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir):
        conf.write_text("later https://open.spotify.com/playlist/AAA0000000000000000000 album\n")
        reconcile_playlists()
        assert (spotdl_dir / "later.album").exists()
        conf.write_text("later https://open.spotify.com/playlist/AAA0000000000000000000\n")
        reconcile_playlists()
        assert not (spotdl_dir / "later.album").exists()


def test_sync_playlists_asks_the_library_per_playlist(tmp_path: Path) -> None:
    """in_library gets the playlist name, and linked tracks are counted (#187)."""
    import unittest.mock as mock

    from music_fetch import ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = _make_conf(tmp_path, ["later  https://open.spotify.com/playlist/L000000000000000000000"])
    asked: list[tuple[str, dict]] = []

    def fake_sync(in_library=None, **_kwargs):
        assert in_library is not None
        assert in_library({"url": "u"}) is True
        return SyncResult(set(), 0, 0, 0, 0, {}, linked=2)

    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(metrics, resolves=lambda song: False,
                              in_library=lambda pl, song: asked.append((pl, song)) or True)

    assert asked == [("later", {"url": "u"})]
    assert metrics.tracks_linked == 2


# ---------------------------------------------------------------------------
# cookie_expiry (issue #166)
# ---------------------------------------------------------------------------


def _cookie_row(name: str, expiry: int, domain: str = ".youtube.com", http_only: bool = False) -> str:
    prefix = "#HttpOnly_" if http_only else ""
    return "\t".join([f"{prefix}{domain}", "TRUE", "/", "TRUE", str(expiry), name, "value"])


def _write_cookies(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_cookie_expiry_parses_httponly_rows(tmp_path: Path) -> None:
    from music_fetch.ingest import cookie_expiry

    path = _write_cookies(tmp_path, [_cookie_row("HSID", 1_800_000_000, http_only=True)])
    assert cookie_expiry(path) == (1_800_000_000, "HSID")


def test_cookie_expiry_ignores_session_cookies(tmp_path: Path) -> None:
    from music_fetch.ingest import cookie_expiry

    path = _write_cookies(tmp_path, [_cookie_row("SID", 0), _cookie_row("SSID", 1_800_000_000)])
    assert cookie_expiry(path) == (1_800_000_000, "SSID")


def test_cookie_expiry_ignores_non_auth_cookies(tmp_path: Path) -> None:
    from music_fetch.ingest import cookie_expiry

    path = _write_cookies(tmp_path, [
        _cookie_row("__Secure-1PSIDTS", 1_700_000_000),
        _cookie_row("SIDCC", 1_700_000_000),
        _cookie_row("VISITOR_INFO1_LIVE", 1_700_000_000),
        _cookie_row("SID", 1_800_000_000),
    ])
    assert cookie_expiry(path) == (1_800_000_000, "SID")


def test_cookie_expiry_soonest_wins(tmp_path: Path) -> None:
    from music_fetch.ingest import cookie_expiry

    path = _write_cookies(tmp_path, [
        _cookie_row("SID", 1_900_000_000),
        _cookie_row("LOGIN_INFO", 1_800_000_000, http_only=True),
        _cookie_row("__Secure-3PSID", 1_850_000_000, domain=".google.com"),
    ])
    assert cookie_expiry(path) == (1_800_000_000, "LOGIN_INFO")


def test_cookie_expiry_no_auth_cookies(tmp_path: Path) -> None:
    from music_fetch.ingest import cookie_expiry

    path = _write_cookies(tmp_path, [_cookie_row("YSC", 0), _cookie_row("PREF", 1_800_000_000)])
    assert cookie_expiry(path) is None


def test_run_pushes_cookie_expiry_when_preflight_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The expiry gauge is pushed even on a run that fails (here: preflight, no Spotify creds)."""
    import unittest.mock as mock
    from music_fetch import ingest

    cookie_file = _write_cookies(tmp_path, [_cookie_row("SAPISID", 1_800_000_000)])
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushes.append((body, job)))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file):
        with pytest.raises(SystemExit):
            ingest.run()

    body = next(body for body, job in pushes if job == "music_ingest")
    assert "music_ingest_cookies_expiry_timestamp_seconds 1800000000" in body
    assert 'reason="auth_spotify"' in body


def test_run_logs_no_auth_cookie_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import unittest.mock as mock
    from music_fetch import ingest

    cookie_file = _write_cookies(tmp_path, [_cookie_row("PREF", 1_800_000_000)])
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushes.append((body, job)))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)

    with mock.patch.object(ingest, "COOKIE_FILE", cookie_file), caplog.at_level("WARNING"):
        with pytest.raises(SystemExit):
            ingest.run()

    assert "No YouTube auth cookie" in caplog.text
    assert all("cookies_expiry" not in body for body, _ in pushes)


def test_sync_playlists_fetches_album_fallback_tracks_in_playlist_order(tmp_path: Path) -> None:
    """An album playlist's fallback tracks share the budget in playlists.conf
    order, and an album playlist without any is skipped (#205)."""
    import unittest.mock as mock

    from music_fetch import albums, ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = tmp_path / "playlists.conf"
    conf.write_text(
        "aaaaaaah  https://open.spotify.com/playlist/A000000000000000000000\n"
        "later     https://open.spotify.com/playlist/L000000000000000000000  album\n"
        "keep      https://open.spotify.com/playlist/K000000000000000000000  album\n"
        "zzz       https://open.spotify.com/playlist/Z000000000000000000000\n",
        encoding="utf-8",
    )
    later_songs = [{"url": f"u{i}", "song_id": f"s{i}", "name": f"T{i}", "artists": ["A"]} for i in range(3)]
    # The album playlist's fallback songs are read from its store.
    for name, songs in (("later", later_songs), ("keep", [])):
        store.save(store.Store.from_songs(name, songs))
    for name in ("later", "keep"):
        (spotdl_dir / f"{name}.album").touch()
    _seed_albums({"a0": {"status": albums.FALLBACK, "fallback_tracks": {"later": ["s2", "s0"]}}})

    calls: list[tuple[str, int | None]] = []

    def fake_sync(name, track_limit=None, **_kwargs):
        calls.append((name, track_limit))
        return SyncResult(set(), 3, 3, 0, 0, {})

    def fake_fallback(songs, output_dir, track_limit=None, in_library=None, **_kwargs):
        calls.append((f"fallback:{output_dir.name}:{','.join(s['song_id'] for s in songs)}", track_limit))
        assert in_library is not None
        return SyncResult(set(), 2, 2, 0, 0, {})

    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.dict("os.environ", {"SYNC_TRACK_LIMIT": "10"}), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync), \
         mock.patch("music_fetch.ingest.download_fallback", side_effect=fake_fallback), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(metrics, resolves=lambda song: False, in_library=lambda pl, song: False)

    assert calls == [("aaaaaaah", 10), ("fallback:later:s0,s2", 7), ("zzz", 5)]
    assert metrics.playlists_skipped == 1  # keep: nothing to fall back on
    # The album tick owns the album playlist's store.
    assert store.load("later").songs == later_songs
    assert store.load("keep").songs == []


def test_spent_budget_still_checks_the_later_playlists(tmp_path: Path) -> None:
    """Once SYNC_TRACK_LIMIT is spent the remaining playlists are synced with a
    limit of 0 (fetched, [HAVE]-linked, store written) instead of skipped;
    they count as deferred (#228)."""
    import unittest.mock as mock
    from music_fetch import ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = tmp_path / "playlists.conf"
    conf.write_text("first   https://open.spotify.com/playlist/A000000000000000000000\nsecond  https://open.spotify.com/playlist/B000000000000000000000\n"
                    "third   https://open.spotify.com/playlist/C000000000000000000000\n", encoding="utf-8")

    calls: list[tuple[str, int | None]] = []

    def fake_sync(name, track_limit=None, **_kwargs):
        calls.append((name, track_limit))
        removed = {"https://open.spotify.com/track/gone"} if name == "third" else set()
        return SyncResult(removed, min(track_limit, 3), min(track_limit, 3), 0, 0, {})

    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.dict("os.environ", {"SYNC_TRACK_LIMIT": "2"}), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(metrics, resolves=lambda song: False)

    assert calls == [("first", 2), ("second", 0), ("third", 0)]
    assert (metrics.playlists_total, metrics.playlists_deferred, metrics.playlists_skipped) == (3, 2, 0)
    assert metrics.tracks_attempted == 2


# ---------------------------------------------------------------------------
# sync_playlists — the playlist store (homelab#1914 stage c)
# ---------------------------------------------------------------------------

PL_URL = "https://open.spotify.com/playlist/P000000000000000000000"
TRACK = "https://open.spotify.com/track/"
LOCAL = {"local": {"id": 7, "path": "/library/local.m4a", "title": "Local", "artist": "Artist"}}


def song(sid: str, **extra) -> dict:
    return {"url": TRACK + sid, "song_id": sid, "name": f"Song {sid}", "artists": ["Artist"], **extra}


def _sync(tmp_path: Path, lines: list[str], result=None, calls=None, fetched=None, sentinels=(),
          fallbacks=None, **kwargs):
    """Run sync_playlists over playlists.conf *lines* with spotdl stubbed out:
    sync_playlist returns *result* (its kwargs go to *calls*), fetch_songs is *fetched*,
    and download_fallback records (its output dir name, songs) in *fallbacks*."""
    import unittest.mock as mock
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir(exist_ok=True)
    for sentinel in sentinels:
        (spotdl_dir / sentinel).touch()
    conf = _make_conf(tmp_path, lines)

    def fake_sync(**kw):
        if calls is not None:
            calls.append(kw)
        return result

    def fake_fallback(songs, output_dir, **_kw):
        if fallbacks is not None:
            fallbacks.append((output_dir.name, songs))
        return SyncResult(set(), len(songs), len(songs), 0, 0, {})

    kwargs.setdefault("resolves", lambda song: False)
    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync), \
         mock.patch("music_fetch.ingest.fetch_songs", side_effect=fetched), \
         mock.patch("music_fetch.ingest.download_fallback", side_effect=fake_fallback), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(metrics, **kwargs)
    return metrics


def _result(**kwargs) -> SyncResult:
    return SyncResult(set(), 0, 0, 0, 0, {}, **kwargs)


def test_sync_writes_each_playlists_store_from_its_conf_url(tmp_path: Path) -> None:
    full_a = song("A", album_name="Full record")
    calls: list = []
    _sync(tmp_path, [f"pl  {PL_URL}"], _result(songs=[song("A"), song("B")], records={TRACK + "A": full_a}),
          calls=calls)
    assert len(calls) == 1
    assert calls[0]["url"] == PL_URL and calls[0]["name"] == "pl"
    assert calls[0]["output_dir"] == tmp_path / "spotdl" / "pl"
    assert calls[0]["previous"] == []
    # Spotify's order and membership, each entry the richest record known.
    assert store.load("pl").songs == [full_a, song("B")]
    assert not list(tmp_path.rglob("*.spotdl"))


def test_sync_keeps_an_earlier_stores_records_and_local_entries(tmp_path: Path) -> None:
    full_b = song("B", album_name="Full record")
    full_c = song("C", album_name="Downloaded")
    store.save(store.Store("pl", [{"song": song("A")}, {"song": full_b}, LOCAL]))
    calls: list = []
    _sync(tmp_path, [f"pl  {PL_URL}"], _result(songs=[song("B"), song("C")], records={TRACK + "C": full_c}),
          calls=calls)
    # The sync judges the want list by the store as it was.
    assert calls[0]["previous"] == [song("A"), full_b]
    # A left Spotify and is gone; B keeps its richer record; the local entry stays last.
    assert store.load("pl").entries == [{"song": full_b}, {"song": full_c}, LOCAL]


def test_sync_downloads_what_the_store_wants(tmp_path: Path) -> None:
    calls: list = []
    _sync(tmp_path, [f"pl  {PL_URL}"], _result(songs=[song("A")]), calls=calls,
          resolves=lambda s: s["song_id"] == "A", held=frozenset({TRACK + "H"}))
    wants = calls[0]["wants"]
    assert not wants(song("A"))  # a library item holds it
    assert not wants(song("H"))  # held: in quarantine or waiting in the inbox
    assert wants(song("C"))


def test_sync_failure_keeps_the_earlier_store(tmp_path: Path) -> None:
    import unittest.mock as mock

    store.save(store.Store.from_songs("pl", [song("A")]))
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    from music_fetch.metrics import IngestMetrics

    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", _make_conf(tmp_path, [f"pl  {PL_URL}"])), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=RuntimeError("429 Too Many Requests")), \
         mock.patch("music_fetch.ingest.time.sleep"):
        with pytest.raises(RuntimeError):
            ingest.sync_playlists(metrics, resolves=lambda song: False)

    assert metrics.success is False and metrics.failure_reason == "rate_limited"
    assert store.load("pl").songs == [song("A")]


def test_sync_builds_a_nosync_store_from_spotify_once(tmp_path: Path, caplog) -> None:
    caplog.set_level("INFO")
    full_a = song("A", album_name="Full record")
    # A store seeded before the freeze lists only what was downloaded: B is on Spotify but not in it.
    store.save(store.Store("pl", [{"song": full_a}, LOCAL]))
    fetched_urls: list[str] = []
    calls: list = []
    metrics = _sync(tmp_path, [f"pl  {PL_URL}  nosync"], calls=calls, sentinels=["pl.nosync"],
                    fetched=lambda url, cookie_file: fetched_urls.append(url) or [song("A"), song("B")],
                    resolves=lambda s: True)
    built = store.load("pl")
    assert fetched_urls == [PL_URL]
    assert calls == []  # never synced by spotdl
    assert metrics.playlists_skipped == 1  # complete: nothing to download
    assert built.source == store.SPOTIFY
    assert built.entries == [{"song": full_a}, {"song": song("B")}, LOCAL]
    assert "[STORE] pl: built from Spotify, 3 entries" in caplog.text

    # Kept from then on: no second fetch.
    caplog.clear()
    _sync(tmp_path, [f"pl  {PL_URL}  nosync"], sentinels=["pl.nosync"], fetched=AssertionError("fetched again"),
          resolves=lambda s: True)
    assert "fetched again" not in caplog.text
    assert "Could not build the store" not in caplog.text
    assert store.load("pl").entries == built.entries


def test_sync_downloads_a_nosync_playlists_want_list_without_rewriting_it(tmp_path: Path, caplog) -> None:
    """A frozen playlist downloads what no library item holds, minus held URLs,
    in store order and from the shared budget; its store stays frozen (homelab#2392)."""
    import unittest.mock as mock

    caplog.set_level("INFO")
    frozen = store.Store.from_songs("frozen", [song("A"), song("B"), song("H"), song("C")])
    frozen.source = store.SPOTIFY
    store.save(frozen)
    calls: list = []
    fallbacks: list = []
    with mock.patch.dict("os.environ", {"SYNC_TRACK_LIMIT": "10"}):
        metrics = _sync(tmp_path, [f"frozen  {PL_URL}  nosync", f"pl  {PL_URL[:-1]}2"], _result(songs=[song("Z")]),
                        calls=calls, fallbacks=fallbacks, sentinels=["frozen.nosync"],
                        fetched=AssertionError("fetched again"), resolves=lambda s: s["song_id"] == "B",
                        held=frozenset({TRACK + "H"}))
    assert fallbacks == [("frozen", [song("A"), song("C")])]
    assert "[FROZEN-WANT] 2 track(s)" in caplog.text
    assert "fetched again" not in caplog.text
    assert store.load("frozen").entries == frozen.entries
    # The next playlist gets what the frozen one left of the budget.
    assert [(c["name"], c["track_limit"]) for c in calls] == [("pl", 8)]
    assert metrics.playlists_skipped == 0 and metrics.tracks_downloaded == 2


def test_sync_nosync_fetch_failure_never_fails_the_sync(tmp_path: Path, caplog) -> None:
    calls: list = []
    metrics = _sync(tmp_path, [f"frozen  {PL_URL}  nosync", f"pl  {PL_URL[:-1]}2"], _result(songs=[song("A")]),
                    calls=calls, sentinels=["frozen.nosync"], fetched=RuntimeError("spotify down"))
    assert "Could not build the store of nosync playlist frozen from Spotify" in caplog.text
    assert store.load("frozen") is None
    # The next playlist still syncs.
    assert [c["name"] for c in calls] == ["pl"]
    assert store.load("pl").songs == [song("A")]
    assert metrics.success is True


def test_sync_playlists_downloads_the_requests_fallback_into_unverified_after_the_playlists(tmp_path: Path) -> None:
    """homelab#2478: an album request's MusicBrainz-built songs go to unverified/request/,
    after the playlists and within the same budget; a held one (waiting there, or rejected)
    isn't fetched again, and there is no [HAVE] check."""
    import unittest.mock as mock

    from music_fetch import album_requests, albums, ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    conf = tmp_path / "playlists.conf"
    conf.write_text("aaaaaaah  https://open.spotify.com/playlist/A000000000000000000000\n", encoding="utf-8")
    songs = [{"url": album_requests.recording_url(f"r{i}"), "song_id": f"r{i}", "name": f"T{i}"} for i in range(3)]
    _seed_albums({"7": {"status": albums.FALLBACK, "origin": albums.REQUEST, "album_songs": songs,
                        "fallback_tracks": {album_requests.SOURCE: ["r0", "r1", "r2"]}}})

    calls: list[tuple[str, int | None]] = []

    def fake_sync(name, track_limit=None, **_kwargs):
        calls.append((name, track_limit))
        return SyncResult(set(), 3, 3, 0, 0, {})

    def fake_fallback(songs, output_dir, track_limit=None, in_library=None, **_kwargs):
        calls.append((f"fallback:{output_dir}:{','.join(s['song_id'] for s in songs)}", track_limit))
        assert in_library is None
        return SyncResult(set(), 2, 2, 0, 0, {})

    metrics = IngestMetrics()
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", conf), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.object(album_requests, "UNVERIFIED", tmp_path / "unverified"), \
         mock.patch.dict("os.environ", {"SYNC_TRACK_LIMIT": "10"}), \
         mock.patch("music_fetch.ingest.sync_playlist", side_effect=fake_sync), \
         mock.patch("music_fetch.ingest.download_fallback", side_effect=fake_fallback), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists(metrics, resolves=lambda song: False, held=frozenset({songs[1]["url"]}),
                              in_library=lambda pl, song: False)

    assert calls == [("aaaaaaah", 10), (f"fallback:{tmp_path / 'unverified' / 'request'}:r0,r2", 7)]
    assert metrics.tracks_attempted == 5

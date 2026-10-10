"""music-ingest: reconcile playlists.conf → daily spotdl sync loop → return pending removals.

On each run:
1. Reconcile disk state with playlists.conf:
   a. Provision new playlists (spotdl save for entries without a .spotdl file).
   b. Reconcile .nosync sentinels.
   c. Queue whole-playlist removals for playlists removed from config.
   d. Delete .spotdl file and download dir for removed playlists.
2. For each remaining .spotdl playlist, in playlists.conf order:
   a. Diff old vs new Spotify URL sets to find removed tracks.
   b. Build its want list (new tracks the library lacks, minus backoff) and
      download from it while SYNC_TRACK_LIMIT lasts.  A spent budget defers
      downloads, never the check (#228).
3. Return a PendingRemovals dataclass for the caller (e.g. the service orchestrator) to
   pass to music-scan for beets source-tag cleanup (soft delete — files stay in library).
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from music_fetch import albums
from music_fetch.config import load_playlists
from music_fetch.metrics import IngestMetrics
from music_fetch.spotdl_ops import download_fallback, find_track_in_snapshot, save_playlist, sync_playlist

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
FAILURES_FILE = SPOTDL_DIR.parent / ".spotdl-failures.json"
PENDING_REMOVALS_PATH = SPOTDL_DIR.parent / ".pending-removals.json"
SPOTIFY_TRACK_URL = "https://open.spotify.com/track/"
COOKIE_FILE = Path("/root/.config/spotdl/cookies.txt")
CONF_PATH = Path("/root/.config/music-pipeline/playlists.conf")
# Google login cookies that gate YouTube access (issue #166, set from MusicGrabber's).
# The *PSIDTS/*PSIDCC cookies are left out: Google rotates them, so the expiry stated
# in an exported file says nothing about when they stop working.
AUTH_COOKIES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
    "LOGIN_INFO",
})


@dataclasses.dataclass
class RemovedTrack:
    title: str
    artist: str
    source: str
    # Identity for matching the library item (#176); None in files queued before.
    spotify_id: str | None = None
    isrc: str | None = None

    @classmethod
    def from_song(cls, song: dict, source: str) -> "RemovedTrack":
        """From a .spotdl song entry."""
        url = song.get("url") or ""
        from_url = url.removeprefix(SPOTIFY_TRACK_URL).split("?")[0] if url.startswith(SPOTIFY_TRACK_URL) else None
        return cls(
            title=song.get("name", ""),
            artist=(song.get("artists") or [""])[0],
            source=source,
            spotify_id=song.get("song_id") or from_url or None,
            isrc=song.get("isrc"),
        )


@dataclasses.dataclass
class PendingRemovals:
    tracks: list[RemovedTrack]
    remove_sources: list[str]


def _deadline_reached(elapsed: float, timeout: int | None) -> bool:
    """Return True if *elapsed* seconds have met or exceeded *timeout*."""
    return timeout is not None and elapsed >= timeout


def classify_failure(error_msg: str) -> str:
    """Map a spotdl error message to a short Prometheus label string."""
    msg = error_msg.lower()
    if "spotify rate-limited" in msg:
        return "spotify_rate_limited"
    if re.search(r"spotifyerror|invalid credentials", msg):
        return "auth_spotify"
    if re.search(r"http error 403|sign in to confirm|cookies", msg):
        return "auth_youtube"
    if re.search(r"429|too many requests", msg):
        return "rate_limited"
    return "spotdl_error"


def preflight() -> str | None:
    """Return a failure-reason string if pre-flight checks fail, else None."""
    if not COOKIE_FILE.exists():
        logger.error("Error: YouTube Premium cookies not found at %s", COOKIE_FILE)
        logger.error("See README for export instructions.")
        return "missing_cookies"

    if not os.environ.get("SPOTIFY_CLIENT_ID") or not os.environ.get("SPOTIFY_CLIENT_SECRET"):
        logger.error("Error: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set")
        return "auth_spotify"

    usage = shutil.disk_usage(Path.home() / "Music")
    free_gb = usage.free / 1024**3
    if free_gb < 1.0:
        logger.error("Error: less than 1 GB free on ~/Music (%.2f GB available)", free_gb)
        return "disk_full"

    return None


def cookie_expiry(path: Path) -> tuple[int, str] | None:
    """Return (soonest expiry, cookie name) among the auth cookies in a Netscape cookies.txt.

    Session cookies (expiry 0) and non-auth cookies are ignored.  Returns None when no auth
    cookie has an expiry.  Never returns or logs a cookie value.
    """
    soonest: tuple[int, str] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7 or fields[5] not in AUTH_COOKIES:
            continue
        try:
            expiry = int(fields[4])
        except ValueError:
            continue
        if expiry > 0 and (soonest is None or expiry < soonest[0]):
            soonest = (expiry, fields[5])
    return soonest


def _record_cookie_expiry(metrics: IngestMetrics) -> None:
    """Log the auth-cookie expiry and set it on ``metrics`` (issue #166).

    A missing file is left to preflight (``missing_cookies``).
    """
    if not COOKIE_FILE.exists():
        return
    try:
        found = cookie_expiry(COOKIE_FILE)
    except OSError:
        logger.warning("Could not read %s for the cookie expiry", COOKIE_FILE, exc_info=True)
        return
    if found is None:
        logger.warning("No YouTube auth cookie with an expiry in %s — was it exported signed in?", COOKIE_FILE)
        return
    expiry, name = found
    metrics.cookies_expiry_timestamp = expiry
    logger.info(
        "YouTube auth cookies expire %s (%s)",
        datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        name,
    )


def _jitter() -> None:
    """Sleep a random interval if SYNC_JITTER_SECONDS is set."""
    import random

    jitter = int(os.environ.get("SYNC_JITTER_SECONDS") or "0")
    if jitter > 0:
        delay = random.randint(0, jitter)
        logger.debug("Jitter: sleeping %d seconds", delay)
        time.sleep(delay)


def reconcile_playlists() -> list[str]:
    """Reconcile disk state with playlists.conf; return list of removed source names.

    - Provisions new playlist entries (spotdl save for entries without .spotdl).
    - Reconciles .nosync sentinels to match config.
    - Detects playlists present on disk but absent from config.
    - Deletes .spotdl file and download dir for removed playlists.

    Returns a list of source names that were removed and should have their beets
    tags cleared by the scan container.  Returns an empty list if playlists.conf
    does not exist (backwards-compatible: no reconciliation occurs).
    """
    if not CONF_PATH.exists():
        logger.warning("playlists.conf not found at %s — skipping declarative reconciliation", CONF_PATH)
        return []

    playlists = load_playlists(CONF_PATH)
    conf_names = {pl.name for pl in playlists}

    # Provision new entries and reconcile .nosync sentinels.
    for pl in playlists:
        spotdl_file = SPOTDL_DIR / f"{pl.name}.spotdl"
        output_dir = SPOTDL_DIR / pl.name
        nosync_file = SPOTDL_DIR / f"{pl.name}.nosync"

        if not spotdl_file.exists():
            logger.info("==> Provisioning new playlist: %s", pl.name)
            output_dir.mkdir(parents=True, exist_ok=True)
            save_playlist(url=pl.url, spotdl_file=spotdl_file)

        if pl.nosync:
            if not nosync_file.exists():
                logger.info("    Creating .nosync sentinel for %s", pl.name)
                nosync_file.touch()
        else:
            if nosync_file.exists():
                logger.info("    Removing .nosync sentinel for %s (nosync flag removed from config)", pl.name)
                nosync_file.unlink()

        album_file = SPOTDL_DIR / f"{pl.name}.album"
        if pl.album and not album_file.exists():
            logger.info("    Creating .album sentinel for %s (downloaded by album mode)", pl.name)
            album_file.touch()
        elif not pl.album and album_file.exists():
            logger.info("    Removing .album sentinel for %s (album flag removed from config)", pl.name)
            album_file.unlink()

    # Detect playlists on disk that are no longer in config.
    existing_names = {f.stem for f in SPOTDL_DIR.glob("*.spotdl")}
    removed_names = existing_names - conf_names

    remove_sources: list[str] = []
    for name in sorted(removed_names):
        logger.info("==> Playlist removed from config: %s — queuing cleanup", name)
        remove_sources.append(name)
        (SPOTDL_DIR / f"{name}.spotdl").unlink(missing_ok=True)
        (SPOTDL_DIR / f"{name}.nosync").unlink(missing_ok=True)
        (SPOTDL_DIR / f"{name}.album").unlink(missing_ok=True)
        download_dir = SPOTDL_DIR / name
        if download_dir.exists():
            shutil.rmtree(download_dir)

    return remove_sources


def _snapshot_songs(sync_data: object) -> list[dict]:
    """The song entries of a parsed .spotdl file ({"type", "query", "songs"})."""
    if not isinstance(sync_data, dict):
        return []
    return sync_data.get("songs") or []


def schedule_unlink(pending: list[RemovedTrack], song: dict, playlist_name: str) -> None:
    """Queue *song*'s removal from *playlist_name* and log it, for both removal paths (#190)."""
    track = RemovedTrack.from_song(song, playlist_name)
    logger.info("  Scheduling unlink: %s — %s (%s)", track.title, track.artist, playlist_name)
    pending.append(track)


def _collect_removals(
    pending: list[RemovedTrack],
    removed_urls: set[str],
    old_songs: list[dict],
    playlist_name: str,
) -> None:
    """Collect removed track info for deferred beets tag cleanup by music-scan."""
    if not removed_urls:
        return

    logger.info(
        "==> Scheduling unlinking of %d removed track(s) from playlist: %s",
        len(removed_urls),
        playlist_name,
    )
    for url in removed_urls:
        entry = find_track_in_snapshot(old_songs, url)
        if entry is None:
            logger.warning("  Could not find snapshot entry for removed URL: %s", url)
            continue
        schedule_unlink(pending, entry, playlist_name)


def sync_playlists(
    remove_sources: list[str],
    metrics: IngestMetrics,
    start: float | None = None,
    in_library: Callable[[str, dict], bool] | None = None,
) -> PendingRemovals:
    """Run the spotdl download loop for all active playlists. Returns pending removals.

    *start* is the monotonic time the overall run began, used for soft-timeout
    accounting.  Defaults to now if not provided (standalone use).

    *in_library(playlist, song)* reports whether the library already holds a new
    track, tagging it with the playlist if so; those tracks are not downloaded (#187).
    """
    if start is None:
        start = time.monotonic()

    track_limit_str = os.environ.get("SYNC_TRACK_LIMIT", "")
    session_budget: int | None = None
    if track_limit_str.strip():
        try:
            session_budget = int(track_limit_str.strip())
        except ValueError:
            logger.error("SYNC_TRACK_LIMIT must be a positive integer, got %r — ignoring", track_limit_str)
    if session_budget is not None and session_budget <= 0:
        logger.error("SYNC_TRACK_LIMIT must be a positive integer, got %d — ignoring", session_budget)
        session_budget = None
    remaining: int | None = session_budget

    if session_budget is not None:
        logger.info("Session track budget: %d new tracks across all playlists", session_budget)

    timeout_str = os.environ.get("SYNC_TIMEOUT_SECONDS", "")
    soft_timeout: int | None = None
    if timeout_str.strip():
        try:
            soft_timeout = int(timeout_str.strip())
        except ValueError:
            logger.error("SYNC_TIMEOUT_SECONDS must be a positive integer, got %r — ignoring", timeout_str)
    if soft_timeout is not None and soft_timeout <= 0:
        logger.error("SYNC_TIMEOUT_SECONDS must be a positive integer, got %d — ignoring", soft_timeout)
        soft_timeout = None

    if soft_timeout is not None:
        logger.info("Soft timeout: %ds — will stop before Kubernetes deadline fires", soft_timeout)

    spotdl_files = sorted(SPOTDL_DIR.glob("*.spotdl"))
    if CONF_PATH.exists():
        try:
            config_order = {pl.name: i for i, pl in enumerate(load_playlists(CONF_PATH))}
            spotdl_files.sort(key=lambda f: (config_order.get(f.stem, len(config_order)), f.stem))
        except Exception:
            pass  # keep alphabetical order on parse failure

    if not spotdl_files:
        logger.info("No .spotdl files found in %s", SPOTDL_DIR)

    pending_removals: list[RemovedTrack] = []
    # Read only: the album tick owns it (#205).
    album_state = albums.State.load(SPOTDL_DIR / albums.STATE_FILE.name)

    for spotdl_file in spotdl_files:
        name = spotdl_file.stem

        # .nosync: skip spotdl sync for frozen playlists
        if (SPOTDL_DIR / f"{name}.nosync").exists():
            logger.info("==> Skipping sync for static playlist: %s (.nosync present)", name)
            metrics.playlists_skipped += 1
            metrics.playlists_total += 1
            continue

        # .album: downloaded whole from Usenet by the album tick.  spotdl only
        # fetches the tracks album mode handed back to it, in playlist order (#205).
        fallback: list[dict] | None = None
        if (SPOTDL_DIR / f"{name}.album").exists():
            try:
                songs = _snapshot_songs(json.loads(spotdl_file.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                songs = []
            fallback = albums.fallback_songs(album_state, name, songs)
            if not fallback:
                logger.info("==> Skipping sync for album playlist: %s (album mode)", name)
                metrics.playlists_skipped += 1
                metrics.playlists_total += 1
                continue

        # Budget exhausted: the playlist is still fetched and checked (removals,
        # [HAVE], backoff); only its downloads wait for the next session (#228).
        if remaining is not None and remaining <= 0:
            logger.info("==> Track budget exhausted — checking %s, deferring its downloads to next session", name)
            metrics.playlists_deferred += 1

        # Soft timeout: stop before the Kubernetes activeDeadlineSeconds fires.
        if _deadline_reached(time.monotonic() - start, soft_timeout):
            logger.info(
                "==> Soft timeout reached (%ds/%ds) — deferring %s to next session",
                int(time.monotonic() - start),
                soft_timeout,
                name,
            )
            metrics.playlists_deferred += 1
            metrics.playlists_total += 1
            continue

        # Validate JSON before we attempt a sync
        try:
            with open(spotdl_file, encoding="utf-8") as fh:
                sync_data = json.load(fh)
        except json.JSONDecodeError:
            logger.warning("WARNING: %s is not valid JSON — skipping", spotdl_file)
            metrics.playlists_total += 1
            continue

        old_songs = _snapshot_songs(sync_data)

        logger.info("==> Syncing playlist: %s", name)
        metrics.playlists_total += 1
        output_dir = SPOTDL_DIR / name
        output_dir.mkdir(parents=True, exist_ok=True)

        playlist_in_library = None if in_library is None else functools.partial(in_library, name)
        track_limit = None if remaining is None else max(remaining, 0)
        try:
            if fallback is not None:
                logger.info("[FALLBACK] %d track(s) album mode could not get from Usenet", len(fallback))
                result = download_fallback(
                    fallback,
                    output_dir=output_dir,
                    cookie_file=COOKIE_FILE,
                    track_limit=track_limit,
                    failures_file=FAILURES_FILE,
                    in_library=playlist_in_library,
                )
            else:
                result = sync_playlist(
                    spotdl_file=spotdl_file,
                    output_dir=output_dir,
                    cookie_file=COOKIE_FILE,
                    track_limit=track_limit,
                    failures_file=FAILURES_FILE,
                    in_library=playlist_in_library,
                )
        except Exception as exc:
            reason = classify_failure(str(exc))
            logger.error(
                "ERROR: spotdl sync failed for %s (reason=%s): %s", name, reason, exc
            )
            metrics.success = False
            metrics.failure_reason = reason
            if reason == "auth_youtube":
                metrics.cookies_expired = True
            raise

        metrics.tracks_attempted += result.attempted
        metrics.tracks_downloaded += result.downloaded
        metrics.tracks_missed += result.missed
        metrics.tracks_failed += result.failed
        metrics.tracks_linked += result.linked
        if remaining is not None:
            # Budget is consumed per attempt: a stuck [MISS] cluster would otherwise loop forever.
            remaining -= result.attempted

        _flag_expired_cookies_from_failures(result.fail_reasons, name, metrics)
        _collect_removals(pending_removals, result.removed_urls, old_songs, name)

        # Brief pause between playlists — avoid hammering Spotify/YouTube APIs.
        time.sleep(5)

    _flag_expired_cookies_from_totals(metrics)

    logger.info("==> music-ingest complete. Run music-scan for local import and playlist generation.")
    return PendingRemovals(tracks=pending_removals, remove_sources=remove_sources)


def _flag_expired_cookies_from_failures(fail_reasons: dict[str, str], playlist: str, metrics: IngestMetrics) -> None:
    """Set ``cookies_expired`` when a per-track [FAIL] reason classifies as auth_youtube (issue #159).

    Per-track download failures never raise — spotdl wraps them and the sync completes —
    so the ``except`` path above never sees a 403 on the media URL.  A 403 is only
    evidence of expired cookies when a cookie file is actually in use.  Logs one WARNING
    per run naming the first offending track.
    """
    if not COOKIE_FILE.exists():
        return
    for url, reason in fail_reasons.items():
        if classify_failure(reason) != "auth_youtube":
            continue
        if not metrics.cookies_expired:
            logger.warning(
                "YouTube cookies at %s look expired: %s failed in playlist %s with %s",
                COOKIE_FILE,
                url,
                playlist,
                reason,
            )
        metrics.cookies_expired = True
        return


def _flag_expired_cookies_from_totals(metrics: IngestMetrics) -> None:
    """Safety net: every attempted track [FAIL]ed and nothing landed on disk (issue #159).

    Catches cookie expiry even when the per-track reason doesn't carry the yt-dlp cause.
    [MISS] tracks (no YouTube source) don't count: they say nothing about cookies.  A total
    YouTube outage trips this too; the response (look at the pod) is the same.
    """
    if metrics.cookies_expired:
        return
    if metrics.tracks_attempted > 0 and metrics.tracks_downloaded == 0 and metrics.tracks_failed == metrics.tracks_attempted:
        logger.warning(
            "All %d attempted track(s) failed to download and none succeeded — "
            "flagging cookies as expired (heuristic: attempted > 0, downloaded == 0, [FAIL] == attempted)",
            metrics.tracks_attempted,
        )
        metrics.cookies_expired = True


def save_pending_removals(pending: PendingRemovals) -> None:
    """Add pending removals to the shared handoff file for music-scan to read.

    Merges with a file not yet consumed: the album tick and music-fetch both write it.
    """
    if not pending.tracks and not pending.remove_sources:
        return
    data: dict = {"tracks": [], "remove_sources": []}
    if PENDING_REMOVALS_PATH.exists():
        try:
            data = json.loads(PENDING_REMOVALS_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Could not read %s — overwriting", PENDING_REMOVALS_PATH)
    data["tracks"] = data.get("tracks", []) + [dataclasses.asdict(t) for t in pending.tracks]
    data["remove_sources"] = sorted(set(data.get("remove_sources", [])) | set(pending.remove_sources))
    PENDING_REMOVALS_PATH.write_text(json.dumps(data), encoding="utf-8")
    logger.info(
        "Saved %d track removal(s) and %d source removal(s) to %s",
        len(pending.tracks),
        len(pending.remove_sources),
        PENDING_REMOVALS_PATH,
    )


def load_pending_removals() -> PendingRemovals | None:
    """Read the pending-removals handoff file without deleting it. Returns None if absent.

    The file stays until :func:`clear_pending_removals` drops what was applied, so a
    crash part-way keeps the removals for the next scan (#259).  An unreadable file is
    moved aside to ``.pending-removals.json.bad``, not retried forever.
    """
    if not PENDING_REMOVALS_PATH.exists():
        return None
    try:
        data = json.loads(PENDING_REMOVALS_PATH.read_text(encoding="utf-8"))
        tracks = [RemovedTrack(**t) for t in data.get("tracks", [])]
        return PendingRemovals(tracks=tracks, remove_sources=data.get("remove_sources", []))
    except Exception:
        bad = PENDING_REMOVALS_PATH.with_name(PENDING_REMOVALS_PATH.name + ".bad")
        logger.warning("Failed to load %s — moved to %s, skipping pending removals",
                       PENDING_REMOVALS_PATH, bad, exc_info=True)
        PENDING_REMOVALS_PATH.replace(bad)
        return None


def clear_pending_removals(applied: PendingRemovals) -> None:
    """Drop the *applied* removals from the handoff file, keeping any queued since it was read.

    The album tick may add removals between the scan's read and this call; those stay
    for the next scan.  The file is deleted once nothing is left.
    """
    if not PENDING_REMOVALS_PATH.exists():
        return
    try:
        data = json.loads(PENDING_REMOVALS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not read %s — deleting it", PENDING_REMOVALS_PATH)
        PENDING_REMOVALS_PATH.unlink(missing_ok=True)
        return
    done = [dataclasses.asdict(t) for t in applied.tracks]
    tracks = []
    for t in data.get("tracks", []):
        # Older files lack the identity keys; compare them as RemovedTrack does.
        key = dataclasses.asdict(RemovedTrack(**t))
        if key in done:
            done.remove(key)
        else:
            tracks.append(t)
    remove_sources = [s for s in data.get("remove_sources", []) if s not in applied.remove_sources]
    if tracks or remove_sources:
        PENDING_REMOVALS_PATH.write_text(json.dumps({"tracks": tracks, "remove_sources": remove_sources}),
                                         encoding="utf-8")
        logger.info("Kept %d track removal(s) and %d source removal(s) queued since the scan read %s",
                    len(tracks), len(remove_sources), PENDING_REMOVALS_PATH)
    else:
        PENDING_REMOVALS_PATH.unlink(missing_ok=True)


@contextmanager
def ingest_run() -> Iterator[IngestMetrics]:
    """One ingest run's metrics, shared by ``run()`` and the nightly flow (#203).

    Records the cookie expiry first and pushes once at the end, failed or not.  Anything
    set here reaches both paths, so a metric can't land in only one of them again.
    """
    metrics = IngestMetrics()
    start = time.monotonic()
    _record_cookie_expiry(metrics)
    try:
        yield metrics
    except Exception:
        metrics.success = False
        if not metrics.failure_reason:
            metrics.failure_reason = "unexpected_error"
        raise
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.push()


def run() -> PendingRemovals:
    """Execute the full ingest pipeline, push metrics on completion, return pending removals."""
    start = time.monotonic()
    with ingest_run() as metrics:
        failure_reason = preflight()
        if failure_reason:
            metrics.success = False
            metrics.failure_reason = failure_reason
            raise SystemExit(1)

        _jitter()

        try:
            logger.info("==> music-ingest starting")
            if FAILURES_FILE.exists():
                try:
                    failures = json.loads(FAILURES_FILE.read_text(encoding="utf-8"))
                    logger.info("Backoff state (%d track(s)):", len(failures))
                    for url, entry in failures.items():
                        logger.info("  [BACK] kind=%s attempts=%d retry_after=%s url=%s reason=%s", entry.get("kind", "miss"), entry.get("attempts", "?"), entry.get("retry_after", "?")[:10], url, entry.get("reason", ""))
                except Exception:
                    logger.warning("Could not read backoff state from %s", FAILURES_FILE)
            else:
                logger.info("Backoff state: empty")

            logger.info("==> Reconciling playlists...")
            try:
                remove_sources = reconcile_playlists()
            except Exception:
                metrics.failure_reason = "reconcile_error"
                logger.exception("Reconciliation step failed")
                raise

            logger.info("==> Syncing playlists...")
            return sync_playlists(remove_sources, metrics, start=start)

        except Exception:
            logger.exception("music-ingest failed")
            raise

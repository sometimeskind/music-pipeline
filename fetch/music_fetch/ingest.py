"""music-ingest: reconcile playlists.conf → daily spotdl sync loop.

On each run:
1. Reconcile disk state with playlists.conf:
   a. Create a new playlist's download dir.
   b. Reconcile the .nosync and .album sentinels.
   c. Delete the store, sentinels, download dir and .m3u of a playlist removed
      from config.  Its items keep their ``sources`` (where they came from).
2. For each spotdl playlist, in playlists.conf order, fetch it from Spotify,
   write its playlist store (homelab#1914) and download its want list (tracks
   no library item holds, minus backoff) while SYNC_TRACK_LIMIT lasts.  A
   spent budget defers downloads, never the check (#228).  A track removed on
   Spotify is gone from the store, so from the playlist; its file stays in
   the library (soft delete).  A ``nosync`` playlist's store is read from
   Spotify once and then kept, but its want list is downloaded the same way
   until it is complete (homelab#2392).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from music_fetch import albums, later_queue, state, store
from music_fetch.config import load_playlists
from music_fetch.metrics import IngestMetrics
from music_fetch.spotdl_ops import _load_failures, download_fallback, fetch_songs, sync_playlist

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
FAILURES_FILE = SPOTDL_DIR.parent / ".spotdl-failures.json"
# music_scan.scan.PLAYLISTS: a playlist removed from config loses its .m3u.
PLAYLISTS_DIR = Path("/root/Music/playlists")
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

    try:
        state.check()
    except state.Unavailable as exc:
        logger.error("[DB-DOWN] The pipeline's state is in the music database, which is down: %s", exc)
        return "state_unavailable"

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
    """Reconcile disk state with playlists.conf; return the playlists removed from it.

    - Creates a new playlist's download dir.  Its store is written by its first
      sync (spotdl), album tick (album) or Spotify fetch (nosync).
    - Reconciles the .nosync and .album sentinels to match config.
    - Deletes the store, sentinels, download dir and .m3u of each playlist that
      has a store but is no longer in config.  Its items' ``sources`` stay:
      they record where an item came from, not what lists it.

    Returns an empty list if playlists.conf does not exist (no reconciliation occurs).
    """
    if not CONF_PATH.exists():
        logger.warning("playlists.conf not found at %s — skipping declarative reconciliation", CONF_PATH)
        return []

    playlists = load_playlists(CONF_PATH)
    conf_names = {pl.name for pl in playlists}

    for pl in playlists:
        output_dir = SPOTDL_DIR / pl.name
        nosync_file = SPOTDL_DIR / f"{pl.name}.nosync"

        if not output_dir.exists():
            logger.info("==> Provisioning new playlist: %s", pl.name)
            output_dir.mkdir(parents=True, exist_ok=True)

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

    removed = sorted(set(store.names()) - conf_names)
    for name in removed:
        logger.info("==> Playlist removed from config: %s — deleting its store and playlist", name)
        store.remove(name)
        (SPOTDL_DIR / f"{name}.nosync").unlink(missing_ok=True)
        (SPOTDL_DIR / f"{name}.album").unlink(missing_ok=True)
        if name != later_queue.PLAYLIST or not state.in_db():
            # later.m3u outlives the later playlist: the Later queue writes it (homelab#2478).
            (PLAYLISTS_DIR / f"{name}.m3u").unlink(missing_ok=True)
        download_dir = SPOTDL_DIR / name
        if download_dir.exists():
            shutil.rmtree(download_dir)

    return removed


def _save_store(name: str, result) -> None:
    """Write playlist *name*'s store from this sync: Spotify's list in order,
    each entry the richest record known (an earlier store's, a download's),
    then the earlier store's local entries."""
    previous = store.load(name)
    known = {} if previous is None else {s.get("url"): s for s in previous.songs}
    playlist = store.Store.from_songs(name, store.merge_spotify(result.songs, {**known, **result.records}),
                                      keep=previous)
    store.save(playlist)


def _build_frozen(name: str, url: str) -> None:
    """A ``nosync`` playlist's store is built once from a Spotify fetch and kept
    from then on (homelab#1914).  Never raises: a failed fetch is retried at the
    next nightly and must not fail this one."""
    try:
        previous = store.load(name)
        if previous is not None and previous.source == store.SPOTIFY:
            return
        known = {} if previous is None else {s.get("url"): s for s in previous.songs}
        playlist = store.Store.from_songs(name, store.merge_spotify(fetch_songs(url, COOKIE_FILE), known),
                                          keep=previous)
        playlist.source = store.SPOTIFY
        store.save(playlist)
        logger.info("[STORE] %s: built from Spotify, %d entries", name, len(playlist.entries))
    except Exception:
        logger.error("Could not build the store of nosync playlist %s from Spotify", name, exc_info=True)


def sync_playlists(
    metrics: IngestMetrics,
    resolves: Callable[[dict], bool],
    held: frozenset[str] = frozenset(),
    start: float | None = None,
    in_library: Callable[[str, dict], bool] | None = None,
) -> None:
    """Run the spotdl download loop for all playlists in playlists.conf.

    *start* is the monotonic time the overall run began, used for soft-timeout
    accounting.  Defaults to now if not provided (standalone use).

    *resolves(song)* reports, without tagging, whether a library item holds the
    track by Spotify ID or ISRC, and *held* lists the URLs whose download waits
    in the inbox or quarantine: a track that is neither is on the playlist's
    want list (homelab#1914).

    *in_library(playlist, song)* reports whether the library already holds a
    wanted track by the full identity ladder, tagging it with the playlist if
    so; those tracks are not downloaded (#187).
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

    playlists = load_playlists(CONF_PATH) if CONF_PATH.exists() else []
    if not playlists:
        logger.info("No playlists in %s", CONF_PATH)

    def wants(song: dict) -> bool:
        return (song.get("url") or "") not in held and not resolves(song)

    # Read only: the album tick owns it (#205).
    album_state = albums.State.load(SPOTDL_DIR / albums.STATE_FILE.name)

    for pl in playlists:
        name = pl.name

        # .nosync: the frozen store is never rewritten from Spotify, but its want
        # list is downloaded until the playlist is complete (homelab#2392).
        fallback: list[dict] | None = None
        frozen = (SPOTDL_DIR / f"{name}.nosync").exists()
        if frozen:
            _build_frozen(name, pl.url)
            playlist = store.load(name)
            fallback = [] if playlist is None else [s for s in playlist.songs if wants(s)]
            if not fallback:
                logger.info("==> Skipping sync for static playlist: %s (.nosync present, nothing wanted)", name)
                metrics.playlists_skipped += 1
                metrics.playlists_total += 1
                continue

        # .album: downloaded whole from Usenet by the album tick.  spotdl only
        # fetches the tracks album mode handed back to it, in playlist order (#205).
        elif (SPOTDL_DIR / f"{name}.album").exists():
            playlist = store.load(name)
            fallback = albums.fallback_songs(album_state, name, [] if playlist is None else playlist.songs)
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

        logger.info("==> Syncing playlist: %s", name)
        metrics.playlists_total += 1
        output_dir = SPOTDL_DIR / name
        output_dir.mkdir(parents=True, exist_ok=True)

        playlist_in_library = None if in_library is None else functools.partial(in_library, name)
        track_limit = None if remaining is None else max(remaining, 0)
        try:
            if fallback is not None:
                if frozen:
                    logger.info("[FROZEN-WANT] %d track(s) on the frozen playlist no library item holds",
                                len(fallback))
                else:
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
                previous = store.load(name)
                result = sync_playlist(
                    url=pl.url,
                    name=name,
                    previous=[] if previous is None else previous.songs,
                    wants=wants,
                    output_dir=output_dir,
                    cookie_file=COOKIE_FILE,
                    track_limit=track_limit,
                    failures_file=FAILURES_FILE,
                    in_library=playlist_in_library,
                )
                _save_store(name, result)
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

        # Brief pause between playlists — avoid hammering Spotify/YouTube APIs.
        time.sleep(5)

    _flag_expired_cookies_from_totals(metrics)

    logger.info("==> music-ingest complete. Run music-scan for local import and playlist generation.")


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


def run() -> None:
    """Execute the full ingest pipeline and push metrics on completion."""
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
            failures = _load_failures(FAILURES_FILE)
            if failures:
                logger.info("Backoff state (%d track(s)):", len(failures))
                for url, entry in failures.items():
                    logger.info("  [BACK] kind=%s attempts=%d retry_after=%s url=%s reason=%s", entry.get("kind", "miss"), entry.get("attempts", "?"), entry.get("retry_after", "?")[:10], url, entry.get("reason", ""))
            else:
                logger.info("Backoff state: empty")

            logger.info("==> Reconciling playlists...")
            try:
                reconcile_playlists()
            except Exception:
                metrics.failure_reason = "reconcile_error"
                logger.exception("Reconciliation step failed")
                raise

            logger.info("==> Syncing playlists...")
            from music_scan.scan import sync_hooks  # noqa: PLC0415 — the image holds both packages

            with sync_hooks() as hooks:
                sync_playlists(metrics, start=start, **hooks)

        except Exception:
            logger.exception("music-ingest failed")
            raise

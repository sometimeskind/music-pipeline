"""music-ingest: reconcile playlists.conf → daily spotdl sync loop → write pending-removals.

On each run:
1. Reconcile disk state with playlists.conf:
   a. Provision new playlists (spotdl save for entries without a .spotdl file).
   b. Reconcile .nosync sentinels.
   c. Queue whole-playlist removals for playlists removed from config.
   d. Delete .spotdl file and download dir for removed playlists.
2. For each remaining .spotdl playlist:
   a. Diff old vs new Spotify URL sets to find removed tracks.
   b. Download new tracks (overwrite=skip ignores already-downloaded files).
   c. Write removed track info to .pending-removals.json on the shared volume.
   music-scan reads this file to clear beets source tags (soft delete — files stay in library).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from music_fetch.config import load_playlists
from music_fetch.metrics import IngestMetrics
from music_fetch.spotdl_ops import find_track_in_snapshot, save_playlist, sync_playlist

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
COOKIE_FILE = Path("/root/.config/spotdl/cookies.txt")
PENDING_REMOVALS = Path("/root/Music/inbox/.pending-removals.json")
CONF_PATH = Path("/root/.config/music-pipeline/playlists.conf")


def _deadline_reached(elapsed: float, timeout: int | None) -> bool:
    """Return True if *elapsed* seconds have met or exceeded *timeout*."""
    return timeout is not None and elapsed >= timeout


def classify_failure(error_msg: str) -> str:
    """Map a spotdl error message to a short Prometheus label string."""
    msg = error_msg.lower()
    if re.search(r"spotifyerror|invalid credentials", msg):
        return "auth_spotify"
    if re.search(r"http error 403|sign in to confirm|cookies", msg):
        return "auth_youtube"
    if re.search(r"429|too many requests", msg):
        return "rate_limited"
    return "spotdl_error"


def _preflight() -> str | None:
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


def _jitter() -> None:
    """Sleep a random interval if SYNC_JITTER_SECONDS is set."""
    import random

    jitter = int(os.environ.get("SYNC_JITTER_SECONDS") or "0")
    if jitter > 0:
        delay = random.randint(0, jitter)
        logger.debug("Jitter: sleeping %d seconds", delay)
        time.sleep(delay)


def _reconcile_playlists() -> list[str]:
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

    # Detect playlists on disk that are no longer in config.
    existing_names = {f.stem for f in SPOTDL_DIR.glob("*.spotdl")}
    removed_names = existing_names - conf_names

    remove_sources: list[str] = []
    for name in sorted(removed_names):
        logger.info("==> Playlist removed from config: %s — queuing cleanup", name)
        remove_sources.append(name)
        (SPOTDL_DIR / f"{name}.spotdl").unlink(missing_ok=True)
        (SPOTDL_DIR / f"{name}.nosync").unlink(missing_ok=True)
        download_dir = SPOTDL_DIR / name
        if download_dir.exists():
            shutil.rmtree(download_dir)

    return remove_sources


def _collect_removals(
    pending: list[dict],
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

        title = entry.get("name", "")
        artists = entry.get("artists", [])
        artist = artists[0] if artists else ""
        logger.info("  Scheduling unlink: %s — %s", title, artist)
        pending.append({"title": title, "artist": artist, "source": playlist_name})


def _write_pending_removals(pending: list[dict], remove_sources: list[str] | None = None) -> None:
    """Write pending source-tag removals to the shared volume for music-scan.

    Writes {"tracks": [...], "remove_sources": [...]} format.
    Merges with any existing file so multiple fetch runs before a scan don't
    overwrite each other.  Supports merging with old list-format files.
    """
    remove_sources = remove_sources or []
    if not pending and not remove_sources:
        return

    PENDING_REMOVALS.parent.mkdir(parents=True, exist_ok=True)

    # Load and merge — safe if fetch runs multiple times before scan processes the file.
    existing_tracks: list[dict] = []
    existing_remove_sources: list[str] = []
    if PENDING_REMOVALS.exists():
        try:
            with open(PENDING_REMOVALS, encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, list):
                # Old format: just a list of track entries.
                existing_tracks = existing
            else:
                existing_tracks = existing.get("tracks", [])
                existing_remove_sources = existing.get("remove_sources", [])
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Could not read %s — discarding prior pending removals: %s", PENDING_REMOVALS, exc)

    merged_tracks = existing_tracks + pending
    merged_remove_sources = existing_remove_sources + remove_sources

    # Atomic write: write to a temp file then rename so a mid-write failure never
    # truncates the existing file.
    tmp = PENDING_REMOVALS.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {"tracks": merged_tracks, "remove_sources": merged_remove_sources},
            fh,
            indent=2,
            ensure_ascii=False,
        )
    tmp.replace(PENDING_REMOVALS)

    logger.info(
        "==> Wrote %d pending track removal(s) and %d source removal(s) to %s",
        len(pending),
        len(remove_sources),
        PENDING_REMOVALS,
    )


def run() -> None:
    """Execute the full ingest pipeline, push metrics on completion."""
    metrics = IngestMetrics()
    start = time.monotonic()

    failure_reason = _preflight()
    if failure_reason:
        metrics.success = False
        metrics.failure_reason = failure_reason
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.push()
        raise SystemExit(1)

    _jitter()

    try:
        logger.info("==> music-ingest starting")

        remove_sources = _reconcile_playlists()

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
        if not spotdl_files:
            logger.info("No .spotdl files found in %s", SPOTDL_DIR)

        pending_removals: list[dict] = []

        for spotdl_file in spotdl_files:
            name = spotdl_file.stem

            # .nosync: skip spotdl sync for frozen playlists
            if (SPOTDL_DIR / f"{name}.nosync").exists():
                logger.info("==> Skipping sync for static playlist: %s (.nosync present)", name)
                metrics.playlists_skipped += 1
                metrics.playlists_total += 1
                continue

            # Budget exhausted: defer remaining playlists to the next session.
            if remaining is not None and remaining <= 0:
                logger.info("==> Track budget exhausted — deferring %s to next session", name)
                metrics.playlists_deferred += 1
                metrics.playlists_total += 1
                continue

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

            old_songs: list[dict] = sync_data if isinstance(sync_data, list) else []

            logger.info("==> Syncing playlist: %s", name)
            metrics.playlists_total += 1
            output_dir = SPOTDL_DIR / name
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                removed_urls, tracks_sent = sync_playlist(
                    spotdl_file=spotdl_file,
                    output_dir=output_dir,
                    cookie_file=COOKIE_FILE,
                    track_limit=remaining,
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

            metrics.tracks_downloaded += tracks_sent
            if remaining is not None:
                remaining -= tracks_sent

            _collect_removals(pending_removals, removed_urls, old_songs, name)

            # Brief pause between playlists — avoid hammering Spotify/YouTube APIs.
            time.sleep(5)

        _write_pending_removals(pending_removals, remove_sources)

        logger.info("==> music-ingest complete. Run music-scan for local import and playlist generation.")

    except SystemExit:
        raise
    except Exception:
        if not metrics.failure_reason:
            metrics.failure_reason = "unexpected_error"
        metrics.success = False
        logger.exception("music-ingest failed")
        raise
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.push()

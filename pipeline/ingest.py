"""music-ingest: daily spotdl sync loop → music-scan.

For each .spotdl playlist:
1. Load the current .spotdl snapshot (old songs).
2. Fetch the current Spotify playlist state (new songs) via spotdl library.
3. Download new tracks (overwrite=skip ignores already-downloaded files).
4. Diff old vs new URL sets to find removed tracks.
5. Clear beets source tags for removed tracks (soft delete — files stay in library).
Then calls music-scan to import + tag + generate playlists.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import pipeline.scan as scan_module
from pipeline.library import MusicLibrary
from pipeline.metrics import IngestMetrics
from pipeline.spotdl_ops import find_track_in_snapshot, sync_playlist

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
COOKIE_FILE = Path("/root/.config/spotdl/cookies.txt")
LIBRARY_DB = Path("/root/.config/beets/library.db")


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

    import shutil
    usage = shutil.disk_usage(Path.home() / "Music")
    free_gb = usage.free / 1024**3
    if free_gb < 1.0:
        logger.error("Error: less than 1 GB free on ~/Music (%.2f GB available)", free_gb)
        return "disk_full"

    return None


def _jitter() -> None:
    """Sleep a random interval if SYNC_JITTER_SECONDS is set."""
    import random

    jitter = int(os.environ.get("SYNC_JITTER_SECONDS", "0"))
    if jitter > 0:
        delay = random.randint(0, jitter)
        logger.debug("Jitter: sleeping %d seconds", delay)
        time.sleep(delay)


def _remove_source_tags(
    lib: MusicLibrary,
    removed_urls: set[str],
    old_songs: list[dict],
    playlist_name: str,
) -> None:
    """Clear beets source tags for all removed track URLs."""
    if not removed_urls:
        return

    logger.info("==> Unlinking %d removed track(s) from playlist: %s", len(removed_urls), playlist_name)
    for url in removed_urls:
        entry = find_track_in_snapshot(old_songs, url)
        if entry is None:
            logger.warning("  Could not find snapshot entry for removed URL: %s", url)
            continue

        title = entry.get("name", "")
        artists = entry.get("artists", [])
        artist = artists[0] if artists else ""
        logger.info("  Unlinking: %s — %s", title, artist)

        found = lib.clear_source_tag(title=title, artist=artist, source=playlist_name)
        if not found:
            logger.warning(
                "  WARNING: not found in beets — may need manual cleanup: %s by %s", title, artist
            )


def run() -> None:
    """Execute the full ingest pipeline, push metrics on completion."""
    import json

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

        track_limit_str = os.environ.get("SYNC_TRACK_LIMIT", "")
        session_budget: int | None = int(track_limit_str) if track_limit_str.strip() else None
        remaining: int | None = session_budget

        if session_budget is not None:
            logger.info("Session track budget: %d new tracks across all playlists", session_budget)

        spotdl_files = sorted(SPOTDL_DIR.glob("*.spotdl"))
        if not spotdl_files:
            logger.info("No .spotdl files found in %s", SPOTDL_DIR)

        with MusicLibrary(LIBRARY_DB) as lib:
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
                    metrics.playlists_skipped += 1
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

                old_songs: list[dict] = sync_data.get("songs", [])

                logger.info("==> Syncing playlist: %s", name)
                metrics.playlists_total += 1
                output_dir = SPOTDL_DIR / name
                output_dir.mkdir(parents=True, exist_ok=True)

                try:
                    removed_urls, downloaded = sync_playlist(
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
                    raise

                if remaining is not None:
                    remaining -= downloaded

                _remove_source_tags(lib, removed_urls, old_songs, name)

                # Brief pause between playlists — avoid hammering Spotify/YouTube APIs.
                time.sleep(5)

        logger.info("==> Sync complete. Calling music-scan for local import and playlist generation...")
        scan_module.run()

        logger.info("==> music-ingest complete")

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

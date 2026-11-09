"""Entry point: music-pipeline service."""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger(__name__)

_AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}


def _start_file_watcher(on_audio_created):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if Path(event.src_path).suffix.lower() in _AUDIO_EXTS:
                logger.debug("File created: %s — scheduling debounced scan", event.src_path)
                on_audio_created()

        # A file written under a temporary name and renamed when complete, as
        # rclone does (homelab#2698), never shows up as a created audio file.
        def on_moved(self, event):
            if not event.is_directory and Path(event.dest_path).suffix.lower() in _AUDIO_EXTS:
                logger.debug("File renamed: %s — scheduling debounced scan", event.dest_path)
                on_audio_created()

    inbox = Path(os.environ.get("MUSIC_INBOX", "/root/Music/inbox"))
    observer = Observer()
    observer.schedule(_Handler(), str(inbox), recursive=True)
    observer.start()
    logger.info("File watcher started on %s", inbox)
    return observer


# How long after the runner starts the first album tick (or sync cycle) runs.
# Leaves the server's recent-deployments scheduler (every 5 s) time to schedule it.
FIRST_ALBUM_TICK_SECONDS = 60
# The two-way playlist sync's cycle (homelab#2361).
PLAYLIST_SYNC_SECONDS = 300


def interval_schedule(interval: int, now: datetime | None = None):
    """An interval schedule (``music-albums``, ``music-playlist-sync``): every
    *interval* seconds, the first a minute after *now* (homelab#2596).

    ``serve()`` upserts the deployment at every pod start; the server then drops
    its pending scheduled runs, and an ``interval=`` schedule is anchored at that
    moment, so its first run came a whole interval later: every restart stopped
    the album tick (and the downloads and imports it starts) for 30 minutes."""
    from prefect.schedules import Interval

    now = now or datetime.now(timezone.utc)
    return Interval(interval, anchor_date=now + timedelta(seconds=FIRST_ALBUM_TICK_SECONDS))


# Waitress's default 4 let four waiting tool calls starve /health past the
# probes' timeout (homelab#2701): each /mcp-api/run call holds a thread for up
# to MCP_INLINE_SECONDS, the short lane takes 5 jobs, and polls come on top.
API_THREADS = 16


def make_api_server(app, host: str = "0.0.0.0", port: int = 8080):
    """The waitress server for the Flask API."""
    import waitress

    return waitress.create_server(app, host=host, port=port, threads=API_THREADS)


def main() -> None:
    missing = [v for v in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "API_BEARER_TOKEN") if not os.environ.get(v)]
    if missing:
        for var in missing:
            logger.error("Required environment variable not set: %s", var)
        sys.exit(1)
    # Set only in an MCP job's subprocess (homelab#2412): anywhere else, a CLI
    # run from this environment would skip the locks it names.
    if os.environ.get("MUSIC_LOCKS_HELD"):
        logger.error("MUSIC_LOCKS_HELD must not be set in the service's environment")
        sys.exit(1)

    from prefect import serve as prefect_serve

    from music_fetch.albums import park_interrupted_imports
    from music_service.api import create_app
    from music_service.debounce import Debouncer
    from music_service.flows import (
        album_import_flow,
        albums_flow,
        fetch_and_scan_flow,
        playlist_sync_flow,
        scan_flow,
    )
    from music_service.mcp_jobs import JobQueue
    from music_service.prefect_client import ensure_concurrency_limits, trigger_scan

    fetch_cron = os.environ.get("FETCH_CRON", "0 3 * * *")

    # Before anything can scan the inbox: an album import the restart cut short
    # goes back to SABnzbd's complete dir, not into the library loose (#344).
    park_interrupted_imports()

    debouncer = Debouncer(delay=30.0, callback=trigger_scan)

    # Flask API in a background daemon thread.
    # Loading the job records marks jobs a restart cut short as interrupted.
    app = create_app(schedule_scan=debouncer.trigger, mcp_jobs=JobQueue())
    api_server = make_api_server(app)
    flask_thread = threading.Thread(target=api_server.run, daemon=True)
    flask_thread.start()
    logger.info("Flask API started on 0.0.0.0:8080")

    # File watcher in background (watchdog starts its own threads).
    observer = _start_file_watcher(debouncer.trigger)

    fetch_deployment = fetch_and_scan_flow.to_deployment(
        name="music-fetch",
        cron=fetch_cron,
    )
    scan_deployment = scan_flow.to_deployment(name="music-scan")
    # Album mode polls Spotify snapshot ids; the flow is a no-op while ALBUM_MODE=off.
    albums_deployment = albums_flow.to_deployment(
        name="music-albums",
        schedule=interval_schedule(int(os.environ.get("ALBUM_POLL_SECONDS", "") or 1800)),
    )
    # The two-way playlist sync (homelab#2361).  Unscheduled while PLAYLIST_SYNC is off: serve()
    # replaces a deployment's schedules, so turning it off stops the cycles.  Any other value is
    # scheduled, and a value the cycle doesn't know is its error outcome, which alerts.
    sync_off = (os.environ.get("PLAYLIST_SYNC") or "off").strip().lower() == "off"
    playlist_sync_deployment = playlist_sync_flow.to_deployment(
        name="music-playlist-sync",
        schedule=None if sync_off else interval_schedule(PLAYLIST_SYNC_SECONDS),
    )
    # Run by /trigger-album-import when SABnzbd finishes an album job.
    album_import_deployment = album_import_flow.to_deployment(name="music-album-import")

    ensure_concurrency_limits()

    logger.info("Starting Prefect runner (FETCH_CRON=%s)", fetch_cron)
    try:
        prefect_serve(fetch_deployment, scan_deployment, albums_deployment, album_import_deployment,
                      playlist_sync_deployment)
    finally:
        debouncer.cancel()
        observer.stop()
        observer.join()

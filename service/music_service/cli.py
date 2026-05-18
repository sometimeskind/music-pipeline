"""Entry point: music-pipeline service."""

from __future__ import annotations

import logging
import os
import sys
import threading
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

    inbox = Path(os.environ.get("MUSIC_INBOX", "/root/Music/inbox"))
    observer = Observer()
    observer.schedule(_Handler(), str(inbox), recursive=True)
    observer.start()
    logger.info("File watcher started on %s", inbox)
    return observer


def main() -> None:
    missing = [v for v in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "API_BEARER_TOKEN") if not os.environ.get(v)]
    if missing:
        for var in missing:
            logger.error("Required environment variable not set: %s", var)
        sys.exit(1)

    from prefect import serve as prefect_serve

    from music_service.api import create_app
    from music_service.debounce import Debouncer
    from music_service.flows import fetch_and_scan_flow, scan_flow
    from music_service.prefect_client import ensure_concurrency_limits, trigger_scan
    import waitress

    fetch_cron = os.environ.get("FETCH_CRON", "0 3 * * *")

    debouncer = Debouncer(delay=30.0, callback=trigger_scan)

    # Flask API in a background daemon thread.
    app = create_app(schedule_scan=debouncer.trigger)
    flask_thread = threading.Thread(
        target=lambda: waitress.serve(app, host="0.0.0.0", port=8080),
        daemon=True,
    )
    flask_thread.start()
    logger.info("Flask API started on 0.0.0.0:8080")

    # File watcher in background (watchdog starts its own threads).
    observer = _start_file_watcher(debouncer.trigger)

    fetch_deployment = fetch_and_scan_flow.to_deployment(
        name="fetch",
        cron=fetch_cron,
    )
    scan_deployment = scan_flow.to_deployment(name="scan")

    ensure_concurrency_limits()

    logger.info("Starting Prefect runner (FETCH_CRON=%s)", fetch_cron)
    try:
        prefect_serve(fetch_deployment, scan_deployment)
    finally:
        debouncer.cancel()
        observer.stop()
        observer.join()

"""Orchestrator: scheduler, file watcher, concurrency lock, and run helpers."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

import music_fetch.ingest as ingest
import music_scan.scan as scan
from music_scan.navidrome import trigger_scan

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}


class _ScanEventHandler(FileSystemEventHandler):
    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orc = orchestrator

    def on_created(self, event: FileCreatedEvent) -> None:
        if Path(event.src_path).suffix.lower() in AUDIO_EXTS:
            logger.debug("File created: %s — scheduling debounced scan", event.src_path)
            self._orc._schedule_debounced_scan()


class Orchestrator:
    def __init__(self, debounce_delay: float = 30.0) -> None:
        self._lock = threading.Lock()
        self._debounce_delay = debounce_delay
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        self._scheduler = BackgroundScheduler()
        self._observer = Observer()

    # ------------------------------------------------------------------
    # Synchronous run methods (used by the scheduler and internally)
    # ------------------------------------------------------------------

    def run_fetch(self) -> None:
        """Acquire lock, run fetch, then immediately run scan with returned pending removals."""
        with self._lock:
            logger.info("==> Fetch starting")
            pending = ingest.run()
            logger.info("==> Fetch complete — starting scan")
            self._run_scan_locked(pending)
            logger.info("==> Scan complete")

    def run_scan(self, pending=None) -> None:
        """Acquire lock and run scan."""
        with self._lock:
            self._run_scan_locked(pending)

    def _run_scan_locked(self, pending=None) -> None:
        """Run scan — caller must already hold self._lock."""
        logger.info("==> Scan starting")
        scan.run(pending)
        pushed = self._push_library()
        if pushed:
            trigger_scan()
        logger.info("==> Scan complete")

    def _push_library(self) -> bool:
        """Push staging and playlists to the configured rclone remote.

        Returns True if a push was performed (LIBRARY_REMOTE is set), False otherwise.
        Callers use the return value to decide whether to trigger a Navidrome rescan —
        a rescan is only useful after files have actually been pushed somewhere.
        """
        remote = os.environ.get("LIBRARY_REMOTE", "")
        if not remote:
            logger.info("LIBRARY_REMOTE not set — skipping library push")
            return False

        staging = Path(os.environ.get("MUSIC_STAGING", "/root/Music/staging"))
        playlists = Path(os.environ.get("MUSIC_PLAYLISTS", "/root/Music/playlists"))

        subprocess.run(["rclone", "copy", str(staging), remote], check=True)
        subprocess.run(["rclone", "sync", str(playlists), f"{remote}/playlists"], check=True)

        logger.info("==> Library pushed to %s", remote)
        return True

    # ------------------------------------------------------------------
    # Non-blocking try methods (used by HTTP trigger endpoints)
    # ------------------------------------------------------------------

    def try_run_fetch(self) -> bool:
        """Acquire lock non-blocking, start fetch+scan in a background thread.

        Returns True if the job was started, False if the lock was already held (busy).
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            t = threading.Thread(target=self._fetch_and_release, daemon=True)
            t.start()
        except Exception:
            self._lock.release()
            raise
        return True

    def try_run_scan(self) -> bool:
        """Acquire lock non-blocking, start scan in a background thread.

        Returns True if the job was started, False if the lock was already held (busy).
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            t = threading.Thread(target=self._scan_and_release, daemon=True)
            t.start()
        except Exception:
            self._lock.release()
            raise
        return True

    def _fetch_and_release(self) -> None:
        try:
            logger.info("==> Fetch starting (triggered)")
            pending = ingest.run()
            logger.info("==> Fetch complete — starting scan")
            self._run_scan_locked(pending)
            logger.info("==> Scan complete")
        except Exception:
            logger.exception("fetch+scan failed")
        finally:
            self._lock.release()

    def _scan_and_release(self, pending=None) -> None:
        try:
            self._run_scan_locked(pending)
        except Exception:
            logger.exception("scan failed")
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # Debounced scan (triggered by file watcher and upload endpoint)
    # ------------------------------------------------------------------

    def schedule_scan(self) -> None:
        """Public method to schedule a debounced scan (e.g. after zip upload)."""
        self._schedule_debounced_scan()

    def _schedule_debounced_scan(self) -> None:
        """Reset the debounce timer; fires try_run_scan() after quiet period."""
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            timer = threading.Timer(self._debounce_delay, self._fire_debounced_scan)
            timer.daemon = True
            self._debounce_timer = timer
            timer.start()

    def _fire_debounced_scan(self) -> None:
        with self._debounce_lock:
            self._debounce_timer = None
        logger.info("==> Debounce timer fired — triggering scan")
        self.try_run_scan()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the APScheduler and watchdog observer."""
        fetch_cron = os.environ.get("FETCH_CRON", "0 3 * * *")
        try:
            minute, hour, day, month, day_of_week = fetch_cron.split()
        except ValueError:
            logger.error(
                "Invalid FETCH_CRON=%r — must be a 5-field cron expression (e.g. '0 3 * * *')", fetch_cron
            )
            raise SystemExit(1)

        self._scheduler.add_job(
            self.run_fetch,
            CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week),
            id="fetch",
        )
        self._scheduler.start()
        logger.info("Scheduler started (FETCH_CRON=%s)", fetch_cron)

        inbox = Path(os.environ.get("MUSIC_INBOX", "/root/Music/inbox"))
        handler = _ScanEventHandler(self)
        self._observer.schedule(handler, str(inbox), recursive=True)
        self._observer.start()
        logger.info("File watcher started on %s", inbox)

    def stop(self) -> None:
        """Shut down scheduler and file watcher."""
        self._scheduler.shutdown(wait=False)
        self._observer.stop()
        self._observer.join()
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

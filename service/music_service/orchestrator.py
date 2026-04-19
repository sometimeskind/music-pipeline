"""Orchestrator: scheduler, file watcher, concurrency lock, and run helpers."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

import music_fetch.ingest as ingest
import music_scan.scan as scan

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}
INBOX = Path(os.environ.get("MUSIC_INBOX", "/root/Music/inbox"))


class _ScanEventHandler(FileSystemEventHandler):
    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orc = orchestrator

    def on_created(self, event: FileCreatedEvent) -> None:
        if isinstance(event, FileCreatedEvent):
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
        logger.info("==> Scan complete")

    # ------------------------------------------------------------------
    # Non-blocking try methods (used by HTTP trigger endpoints)
    # ------------------------------------------------------------------

    def try_run_fetch(self) -> bool:
        """Acquire lock non-blocking, start fetch+scan in a background thread.

        Returns True if the job was started, False if the lock was already held (busy).
        """
        if not self._lock.acquire(blocking=False):
            return False
        t = threading.Thread(target=self._fetch_and_release, daemon=True)
        t.start()
        return True

    def try_run_scan(self) -> bool:
        """Acquire lock non-blocking, start scan in a background thread.

        Returns True if the job was started, False if the lock was already held (busy).
        """
        if not self._lock.acquire(blocking=False):
            return False
        t = threading.Thread(target=self._scan_and_release, daemon=True)
        t.start()
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
        minute, hour, day, month, day_of_week = fetch_cron.split()
        self._scheduler.add_job(
            self.run_fetch,
            CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week),
            id="fetch",
        )
        self._scheduler.start()
        logger.info("Scheduler started (FETCH_CRON=%s)", fetch_cron)

        handler = _ScanEventHandler(self)
        self._observer.schedule(handler, str(INBOX), recursive=True)
        self._observer.start()
        logger.info("File watcher started on %s", INBOX)

    def stop(self) -> None:
        """Shut down scheduler and file watcher."""
        self._scheduler.shutdown(wait=False)
        self._observer.stop()
        self._observer.join()
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

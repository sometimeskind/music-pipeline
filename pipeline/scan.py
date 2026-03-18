"""music-scan: import inbox → beets, refresh metadata, regenerate .m3u playlists,
trigger Navidrome rescan, push Prometheus metrics.

Called frequently (every 5 min by default) and also by music-ingest after a sync.
No Spotify or YouTube calls.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from pipeline.library import MusicLibrary
from pipeline.metrics import ScanMetrics
from pipeline.navidrome import trigger_rescan
from pipeline.process import run_beet_import, run_beet_update

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
QUARANTINE = Path("/root/Music/quarantine")
PLAYLISTS = Path("/root/Music/playlists")
INBOX = Path("/root/Music/inbox")
LIBRARY_DB = Path("/root/.config/beets/library.db")


def _count_quarantine() -> int:
    if not QUARANTINE.exists():
        return 0
    return sum(1 for _ in QUARANTINE.rglob("*") if _.is_file())


def _quarantine_inbox_leftovers() -> int:
    """Move any audio files still in the inbox root to quarantine.

    After ``beet import`` has processed everything it can, un-matched audio
    files remain in the inbox.  We move them to quarantine for manual review.
    Returns the count of files moved.
    """
    audio_exts = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in INBOX.glob("*"):
        if f.is_file() and f.suffix.lower() in audio_exts:
            dest = QUARANTINE / f.name
            f.rename(dest)
            moved += 1
    return moved


def _regen_playlists() -> None:
    """Regenerate .m3u files for every .spotdl playlist."""
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    spotdl_files = sorted(SPOTDL_DIR.glob("*.spotdl"))
    if not spotdl_files:
        logger.debug("No .spotdl files found — no playlists to generate")
        return

    with MusicLibrary(LIBRARY_DB) as lib:
        for spotdl_file in spotdl_files:
            name = spotdl_file.stem
            m3u = PLAYLISTS / f"{name}.m3u"
            logger.info("    Generating: %s", m3u)
            paths = lib.paths_by_source(name)
            lines = [os.path.relpath(p, PLAYLISTS) for p in sorted(paths)]
            m3u.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def run() -> None:
    """Execute the full scan pipeline, push metrics on completion."""
    metrics = ScanMetrics()
    start = time.monotonic()

    try:
        logger.info("==> music-scan starting")

        quarantined_before = _count_quarantine()

        logger.info("==> Importing from inbox...")
        run_beet_import(INBOX)

        logger.info("==> Quarantining skipped files...")
        moved = _quarantine_inbox_leftovers()
        logger.info("Quarantined : %d file(s) → %s", moved, QUARANTINE)
        logger.info("Log         : ~/.config/beets/import.log")

        logger.info("==> Refreshing library metadata...")
        run_beet_update()

        logger.info("==> Regenerating playlists...")
        _regen_playlists()

        quarantined_after = _count_quarantine()
        metrics.quarantined_tracks = max(0, quarantined_after - quarantined_before)

        metrics.navidrome_rescan_success = trigger_rescan()

        logger.info("==> music-scan complete")

    except Exception:
        metrics.success = False
        metrics.failure_reason = "unexpected_error"
        logger.exception("music-scan failed")
        raise
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.push()

"""Prefect tasks and flows for the music pipeline."""

from __future__ import annotations

import time

from prefect import flow, get_run_logger, task
from prefect.concurrency.sync import concurrency

import music_fetch.ingest as ingest
import music_scan.reconcile as reconcile
import music_scan.scan as scan
from music_fetch.config import load_playlists
from music_fetch.metrics import IngestMetrics


# ---------------------------------------------------------------------------
# Fetch tasks
# ---------------------------------------------------------------------------


@task(name="preflight", log_prints=True)
def preflight_task() -> None:
    """Check cookies, Spotify credentials, and disk space. Raises on failure."""
    logger = get_run_logger()
    reason = ingest.preflight()
    if reason:
        raise RuntimeError(f"Preflight failed: {reason}")
    logger.info("Preflight passed: cookies, credentials, and disk space OK")


@task(name="reconcile-playlists", log_prints=True)
def reconcile_playlists_task() -> list[str]:
    """Reconcile playlists.conf: provision new entries, queue removed ones."""
    logger = get_run_logger()

    if ingest.CONF_PATH.exists():
        try:
            playlists = load_playlists(ingest.CONF_PATH)
            active = [p for p in playlists if not p.nosync]
            nosync = [p for p in playlists if p.nosync]
            logger.info(
                "Playlists in config: %d total (%d active, %d nosync)",
                len(playlists),
                len(active),
                len(nosync),
            )
            for p in active:
                logger.info("  [active] %s", p.name)
            for p in nosync:
                logger.info("  [nosync] %s", p.name)
        except Exception as exc:
            logger.warning("Could not pre-read playlists.conf: %s", exc)

    remove_sources = ingest.reconcile_playlists()
    if remove_sources:
        logger.info("Removed from config (queued for cleanup): %s", ", ".join(remove_sources))
    else:
        logger.info("No playlists removed from config")
    return remove_sources


@task(name="spotdl-sync", log_prints=True, persist_result=False)
def spotdl_sync_task(remove_sources: list[str]):
    """Run spotdl sync for all active playlists. Returns PendingRemovals."""
    logger = get_run_logger()
    metrics = IngestMetrics()
    start = time.monotonic()
    try:
        result = ingest.sync_playlists(remove_sources, metrics)
        logger.info(
            "Sync complete: %d of %d track(s) downloaded, %d playlist(s) processed, %d pending removal(s)",
            metrics.tracks_downloaded,
            metrics.tracks_attempted,
            metrics.playlists_total,
            len(result.tracks),
        )
        if metrics.playlists_skipped:
            logger.info("  %d nosync playlist(s) skipped", metrics.playlists_skipped)
        if metrics.playlists_deferred:
            logger.info("  %d playlist(s) deferred (budget/timeout)", metrics.playlists_deferred)
        return result
    except Exception:
        metrics.success = False
        if not metrics.failure_reason:
            metrics.failure_reason = "unexpected_error"
        raise
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.push()


# ---------------------------------------------------------------------------
# Scan tasks
# ---------------------------------------------------------------------------


@task(name="save-removals", log_prints=True)
def save_removals_task(pending) -> None:
    """Persist pending removals to disk for the scan flow to consume."""
    logger = get_run_logger()
    if pending.tracks or pending.remove_sources:
        ingest.save_pending_removals(pending)
        logger.info(
            "Saved %d track removal(s), %d source removal(s)",
            len(pending.tracks),
            len(pending.remove_sources),
        )
    else:
        logger.info("No pending removals")


@task(name="apply-removals", log_prints=True)
def apply_removals_task() -> None:
    """Clear beets source tags for tracks removed from Spotify playlists."""
    logger = get_run_logger()
    pending = ingest.load_and_clear_pending_removals()
    if pending is None:
        logger.info("No pending removals")
        return
    logger.info(
        "Applying removals: %d track(s), %d full-source removal(s)",
        len(pending.tracks),
        len(pending.remove_sources),
    )
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    with MusicLibrary(scan.LIBRARY_DB) as lib:
        count = scan.apply_pending_removals(pending, lib)
    logger.info("Cleared %d beets entry/entries", count)


@task(name="beet-import", log_prints=True)
def beet_import_task() -> list:
    """Import inbox audio files into the beets library."""
    logger = get_run_logger()
    with concurrency("beet-import", occupy=1):
        imported = scan.run_inbox_import()
    logger.info("Imported %d track(s) from inbox", len(imported))
    for title, artist in imported[:10]:
        logger.info("  + %s — %s", title, artist)
    if len(imported) > 10:
        logger.info("  ... and %d more", len(imported) - 10)
    return imported


@task(name="quarantine-leftovers", log_prints=True)
def quarantine_task() -> None:
    """Move unmatched inbox audio files to quarantine for manual review."""
    logger = get_run_logger()
    moved = scan.quarantine_inbox_leftovers()
    if moved:
        logger.info("Quarantined %d unmatched file(s) for manual review", moved)
    else:
        logger.info("No unmatched files left in inbox")


@task(name="asis-import", log_prints=True)
def asis_import_task() -> None:
    """Import quarantine files that already have complete tags (--asis)."""
    logger = get_run_logger()
    count = scan.import_asis_from_quarantine()
    logger.info("Asis import: %d track(s) imported from quarantine", count)


@task(name="beet-update", log_prints=True)
def beet_update_task() -> None:
    """Refresh beets library metadata."""
    logger = get_run_logger()
    from music_scan.process import run_beet_update  # noqa: PLC0415
    run_beet_update()
    logger.info("Beets library metadata refreshed")


@task(name="regen-playlists", log_prints=True)
def regen_playlists_task() -> None:
    """Regenerate .m3u playlist files from the beets library."""
    logger = get_run_logger()
    counts = scan.regen_playlists() or {}
    if counts:
        total = sum(counts.values())
        logger.info("Regenerated %d playlist(s), %d total track(s)", len(counts), total)
        for name, count in sorted(counts.items()):
            logger.info("  %s: %d track(s)", name, count)
    else:
        logger.info("No playlists to regenerate")


@task(name="navidrome-rescan", log_prints=True)
def navidrome_task() -> None:
    """Trigger Navidrome library rescan."""
    logger = get_run_logger()
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    trigger_scan()
    logger.info("Navidrome rescan triggered")


@task(name="reconcile-snapshots", log_prints=True)
def reconcile_task() -> None:
    """Drop stale URLs from .spotdl snapshots so spotdl re-downloads them next fetch."""
    logger = get_run_logger()
    dropped = reconcile.reconcile_all()
    if dropped:
        logger.info("Dropped %d stale URL(s) from snapshots — will re-download next fetch", dropped)
    else:
        logger.info("All snapshot URLs verified — no stale entries found")


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


@flow(name="fetch", log_prints=True)
def fetch_and_scan_flow() -> None:
    """Fetch only: spotdl sync. Scan is triggered separately by the file watcher."""
    preflight_task()
    remove_sources = reconcile_playlists_task()
    pending = spotdl_sync_task(remove_sources)
    save_removals_task(pending)


@flow(name="scan", log_prints=True)
def scan_flow() -> None:
    """Scan: apply any pending removals, import inbox, regenerate playlists."""
    apply_removals_task()
    beet_import_task()
    quarantine_task()
    asis_import_task()
    beet_update_task()
    regen_playlists_task()
    navidrome_task()
    reconcile_task()

"""Prefect tasks and flows for the music pipeline."""

from __future__ import annotations

import time

from prefect import concurrency, flow, task

import music_fetch.ingest as ingest
import music_scan.reconcile as reconcile
import music_scan.scan as scan
from music_fetch.metrics import IngestMetrics


# ---------------------------------------------------------------------------
# Fetch tasks
# ---------------------------------------------------------------------------


@task(name="preflight", log_prints=True)
def preflight_task() -> None:
    """Check cookies, Spotify credentials, and disk space. Raises on failure."""
    reason = ingest.preflight()
    if reason:
        raise RuntimeError(f"Preflight failed: {reason}")


@task(name="reconcile-playlists", log_prints=True)
def reconcile_playlists_task() -> list[str]:
    """Reconcile playlists.conf: provision new entries, queue removed ones."""
    return ingest.reconcile_playlists()


@task(name="spotdl-sync", log_prints=True, persist_result=False)
def spotdl_sync_task(remove_sources: list[str]):
    """Run spotdl sync for all active playlists. Returns PendingRemovals."""
    metrics = IngestMetrics()
    start = time.monotonic()
    try:
        result = ingest.sync_playlists(remove_sources, metrics)
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


@task(name="apply-removals", log_prints=True)
def apply_removals_task(pending=None) -> None:
    """Clear beets source tags for tracks removed from Spotify playlists."""
    if pending is None:
        return
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    with MusicLibrary(scan.LIBRARY_DB) as lib:
        scan.apply_pending_removals(pending, lib)


@task(name="beet-import", log_prints=True)
def beet_import_task() -> list:
    """Import inbox audio files into the beets library."""
    with concurrency("beet-import", occupy=1):
        return scan.run_inbox_import()


@task(name="quarantine-leftovers", log_prints=True)
def quarantine_task() -> None:
    """Move unmatched inbox audio files to quarantine for manual review."""
    scan.quarantine_inbox_leftovers()


@task(name="asis-import", log_prints=True)
def asis_import_task() -> None:
    """Import quarantine files that already have complete tags (--asis)."""
    scan.import_asis_from_quarantine()


@task(name="beet-update", log_prints=True)
def beet_update_task() -> None:
    """Refresh beets library metadata."""
    from music_scan.process import run_beet_update  # noqa: PLC0415
    run_beet_update()


@task(name="regen-playlists", log_prints=True)
def regen_playlists_task() -> None:
    """Regenerate .m3u playlist files from the beets library."""
    scan.regen_playlists()


@task(name="navidrome-rescan", log_prints=True)
def navidrome_task() -> None:
    """Trigger Navidrome library rescan."""
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    trigger_scan()


@task(name="reconcile-snapshots", log_prints=True)
def reconcile_task() -> None:
    """Drop stale URLs from .spotdl snapshots so spotdl re-downloads them next fetch."""
    reconcile.reconcile_all()


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


@flow(name="fetch-and-scan", log_prints=True)
def fetch_and_scan_flow() -> None:
    """Full pipeline: spotdl fetch then beets scan."""
    preflight_task()
    remove_sources = reconcile_playlists_task()
    pending = spotdl_sync_task(remove_sources)
    apply_removals_task(pending)
    beet_import_task()
    quarantine_task()
    asis_import_task()
    beet_update_task()
    regen_playlists_task()
    navidrome_task()
    reconcile_task()


@flow(name="scan", log_prints=True)
def scan_flow() -> None:
    """Scan only: import inbox and regenerate playlists."""
    beet_import_task()
    quarantine_task()
    asis_import_task()
    beet_update_task()
    regen_playlists_task()
    navidrome_task()
    reconcile_task()

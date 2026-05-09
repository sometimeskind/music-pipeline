"""Prefect tasks and flows for the music pipeline."""

from __future__ import annotations

from prefect import flow, task

import music_fetch.ingest as ingest
import music_scan.reconcile as reconcile
import music_scan.scan as scan


@task(name="fetch", log_prints=True, persist_result=False)
def fetch_task():
    """Reconcile playlists.conf, run spotdl sync. Returns PendingRemovals."""
    return ingest.run()


@task(name="scan", log_prints=True, persist_result=False)
def scan_task(pending=None):
    """Import inbox → beets, quarantine leftovers, regen .m3u, trigger Navidrome."""
    scan.run(pending)


@task(name="reconcile-snapshots", log_prints=True)
def reconcile_task():
    """Drop stale URLs from .spotdl snapshots so spotdl re-downloads them next fetch."""
    reconcile.reconcile_all()


@flow(name="fetch-and-scan", log_prints=True)
def fetch_and_scan_flow():
    """Full pipeline: spotdl fetch then beets scan."""
    pending = fetch_task()
    scan_task(pending)
    reconcile_task()


@flow(name="scan", log_prints=True)
def scan_flow():
    """Scan only: import inbox, regenerate playlists."""
    scan_task(None)
    reconcile_task()

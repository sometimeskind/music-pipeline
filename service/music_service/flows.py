"""Prefect tasks and flows for the music pipeline."""

from __future__ import annotations

import json
import logging
import time

from prefect import flow, get_run_logger, task
from prefect.concurrency.sync import concurrency

import music_fetch.ingest as ingest
import music_scan.reconcile as reconcile
import music_scan.scan as scan
from music_fetch.config import load_playlists
from music_fetch.metrics import IngestMetrics
from music_scan.metrics import ScanMetrics

# Prefect runs each flow run in a `python -m prefect.engine` subprocess where its own
# logging dictConfig owns the root logger (level WARNING). music_fetch / music_scan
# loggers inherit that, so their INFO records (per-track [OK]/[MISS]/[FAIL] lines)
# would be dropped before reaching the console handler or the API handler attached
# via PREFECT_LOGGING_EXTRA_LOGGERS. This module is the deployment entrypoint and is
# imported in every flow-run process, so pin those loggers to INFO here.
for _name in ("music_fetch", "music_scan"):
    logging.getLogger(_name).setLevel(logging.INFO)


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
        if ingest.FAILURES_FILE.exists():
            try:
                failures = json.loads(ingest.FAILURES_FILE.read_text(encoding="utf-8"))
                logger.info("Backoff state: %d track(s) backed off", len(failures))
                for url, entry in failures.items():
                    logger.info(
                        "  [BACK] kind=%s attempts=%d retry_after=%s url=%s",
                        entry.get("kind", "miss"),
                        entry.get("attempts", "?"),
                        entry.get("retry_after", "?")[:10],
                        url,
                    )
            except Exception:
                logger.warning("Could not read backoff state from %s", ingest.FAILURES_FILE)
        else:
            logger.info("Backoff state: empty")
        result = ingest.sync_playlists(remove_sources, metrics)
        not_downloaded = metrics.tracks_attempted - metrics.tracks_downloaded
        suffix = ""
        if not_downloaded:
            parts = []
            if metrics.tracks_missed:
                parts.append(f"{metrics.tracks_missed} no source")
            if metrics.tracks_failed:
                parts.append(f"{metrics.tracks_failed} download error")
            if parts:
                suffix = f" ({', '.join(parts)})"
        logger.info(
            "Sync complete: %d of %d track(s) downloaded%s, %d playlist(s) processed, %d pending removal(s)",
            metrics.tracks_downloaded,
            metrics.tracks_attempted,
            suffix,
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
def apply_removals_task() -> int:
    """Clear beets source tags for tracks removed from Spotify playlists."""
    logger = get_run_logger()
    pending = ingest.load_and_clear_pending_removals()
    if pending is None:
        logger.info("No pending removals")
        return 0
    logger.info(
        "Applying removals: %d track(s), %d full-source removal(s)",
        len(pending.tracks),
        len(pending.remove_sources),
    )
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    with MusicLibrary(scan.LIBRARY_DB) as lib:
        count = scan.apply_pending_removals(pending, lib)
    logger.info("Cleared %d beets entry/entries", count)
    return count


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
def quarantine_task() -> int:
    """Move unmatched inbox audio files to quarantine for manual review."""
    logger = get_run_logger()
    moved = scan.quarantine_inbox_leftovers()
    if moved:
        logger.info("Quarantined %d unmatched file(s) for manual review", moved)
    else:
        logger.info("No unmatched files left in inbox")
    return moved


@task(name="asis-import", log_prints=True)
def asis_import_task() -> int:
    """Import quarantine files that already have complete tags (--asis)."""
    logger = get_run_logger()
    count = scan.import_asis_from_quarantine()
    logger.info("Asis import: %d track(s) imported from quarantine", count)
    return count


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


def _run_scan_tasks() -> None:
    """Every scan pushes the music_scan job, failed or not, so its alerts see it."""
    metrics = ScanMetrics()
    start = time.monotonic()
    try:
        metrics.tracks_removed = apply_removals_task()
        imported = beet_import_task()
        metrics.quarantined_tracks = quarantine_task()
        metrics.tracks_imported = len(imported) + asis_import_task()
        beet_update_task()
        regen_playlists_task()
        try:
            navidrome_task()
        except Exception:
            metrics.failure_reason = "navidrome_trigger_failed"
            raise
        reconcile_task()
    except Exception:
        metrics.success = False
        if not metrics.failure_reason:
            metrics.failure_reason = "unexpected_error"
        raise
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.lossless_items = scan.count_lossless_items()
        metrics.push()


# Flow names are prefixed because the document-pipeline service serves its
# own flows against the same Prefect server. Prefect keys a deployment on
# flow name + deployment name, so a bare "scan" resolved to one shared
# record that both runners polled and each crashed on the other's
# entrypoint. "fetch" is prefixed too so it cannot repeat that.
@flow(name="music-fetch", log_prints=True)
def fetch_and_scan_flow() -> None:
    """Fetch: spotdl sync, then scan inbox."""
    with concurrency("pipeline", occupy=1):
        preflight_task()
        remove_sources = reconcile_playlists_task()
        pending = spotdl_sync_task(remove_sources)
        save_removals_task(pending)
        _run_scan_tasks()


@flow(name="music-scan", log_prints=True)
def scan_flow() -> None:
    """Scan: apply any pending removals, import inbox, regenerate playlists."""
    logger = get_run_logger()
    try:
        with concurrency("pipeline", occupy=1, timeout_seconds=0):
            _run_scan_tasks()
    except TimeoutError:
        logger.info("Scan skipped — pipeline busy (fetch or scan already running)")


# ---------------------------------------------------------------------------
# Album mode (#168)
# ---------------------------------------------------------------------------


def _album_library_hooks(lib):
    """have / complete callbacks over one open beets library.

    ``have`` caches each playlist's track keys for a tick; ``complete`` imports
    a finished download and drops the cache, since the import changed the library.
    """
    import music_fetch.albums as albums  # noqa: PLC0415

    keys: dict[str, set] = {}

    def have(playlist: str, tracks: list[list[str]]) -> bool:
        if playlist not in keys:
            keys[playlist] = scan.source_track_keys(lib, playlist)
        return scan.has_tracks(keys[playlist], tracks)

    def fresh_have(playlist: str, tracks: list[list[str]]) -> bool:
        return scan.has_tracks(scan.source_track_keys(lib, playlist), tracks)

    def import_inbox() -> None:
        # Waits for a running fetch or scan: beets' SQLite has one writer.
        with concurrency("pipeline", occupy=1):
            _run_scan_tasks()

    def add_source(have_source: str, new_source: str, tracks: list[list[str]]) -> None:
        scan.add_source(lib, have_source, new_source, tracks)

    def complete(state, completion) -> str | None:
        status = albums.complete(state, completion, import_inbox, fresh_have, add_source)
        keys.clear()
        return status

    return have, complete


@task(name="album-tick", log_prints=True)
def album_tick_task(settings) -> None:
    """Refresh album playlists, recover lost import triggers and top the Usenet queue up."""
    import music_fetch.albums as albums  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415
    from music_fetch.usenet import Prowlarr, Sabnzbd  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415

    logger = get_run_logger()
    playlists = [(p.name, p.url) for p in load_playlists(ingest.CONF_PATH) if p.album]
    if not playlists:
        logger.info("No playlists flagged album in playlists.conf")
        return

    with MusicLibrary(scan.LIBRARY_DB) as lib:
        have, complete = _album_library_hooks(lib)
        result = albums.tick(
            playlists,
            SpotifyPlaylists(ingest.COOKIE_FILE),
            Prowlarr(),
            Sabnzbd(),
            have,
            ingest.SPOTDL_DIR,
            settings,
            on_completion=complete,
        )

    removed = [
        ingest.RemovedTrack(title=s.get("name", ""), artist=(s.get("artists") or [""])[0], source=name)
        for name, songs in result.removed_songs.items()
        for s in songs
    ]
    if removed:
        ingest.save_pending_removals(ingest.PendingRemovals(tracks=removed, remove_sources=[]))
        logger.info("Queued %d track removal(s) from album playlists for the next scan", len(removed))


@flow(name="music-albums", log_prints=True)
def albums_flow() -> None:
    """Album mode: download album-only playlists as whole albums from Usenet."""
    import music_fetch.albums as albums  # noqa: PLC0415

    logger = get_run_logger()
    settings = albums.Settings.from_env()
    if settings.mode == "off":
        logger.info("Album mode is off (ALBUM_MODE)")
        return
    try:
        with concurrency("albums", occupy=1, timeout_seconds=0):
            album_tick_task(settings)
    except TimeoutError:
        logger.info("Album tick skipped — another album run holds the lock")


@flow(name="music-album-import", log_prints=True)
def album_import_flow(nzo_id: str, ok: bool, path: str = "", fail_message: str = "") -> None:
    """A SABnzbd album job finished (/trigger-album-import): import it or
    blocklist the release, then grab the next album."""
    import music_fetch.albums as albums  # noqa: PLC0415
    from music_fetch.usenet import Prowlarr, Sabnzbd  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415

    logger = get_run_logger()
    settings = albums.Settings.from_env()
    # Waits for a tick: both read and write .albums.json.
    with concurrency("albums", occupy=1):
        state = albums.State.load()
        success = False
        try:
            with MusicLibrary(scan.LIBRARY_DB) as lib:
                have, complete = _album_library_hooks(lib)
                status = complete(state, albums.Completion(nzo_id=nzo_id, ok=ok, path=path, fail_message=fail_message))
                if status is not None and settings.mode == "on":
                    albums.top_up(state, settings, Prowlarr(), Sabnzbd(), have, albums.TickResult())
            success = True
        finally:
            state.save()
            albums.push_metrics(state, success)
    logger.info("Album import for %s: %s", nzo_id, status or "not an album-mode job")

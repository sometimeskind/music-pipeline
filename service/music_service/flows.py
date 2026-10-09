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
def preflight_task(metrics) -> None:
    """Check cookies, Spotify credentials, and disk space. Raises on failure."""
    logger = get_run_logger()
    reason = ingest.preflight()
    if reason:
        metrics.failure_reason = reason
        raise RuntimeError(f"Preflight failed: {reason}")
    logger.info("Preflight passed: cookies, credentials, and disk space OK")


@task(name="reconcile-playlists", log_prints=True)
def reconcile_playlists_task(metrics) -> list[str]:
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

    try:
        remove_sources = ingest.reconcile_playlists()
    except Exception:
        metrics.failure_reason = "reconcile_error"
        raise
    if remove_sources:
        logger.info("Removed from config (queued for cleanup): %s", ", ".join(remove_sources))
    else:
        logger.info("No playlists removed from config")
    return remove_sources


@task(name="spotdl-sync", log_prints=True, persist_result=False)
def spotdl_sync_task(remove_sources: list[str], metrics):
    """Run spotdl sync for all active playlists. Returns PendingRemovals.

    Tracks the library already holds (by Spotify ID or ISRC) are tagged with the
    playlist instead of downloaded (#187).  The index is built once: a track
    downloaded earlier in this run isn't in the library until the scan.
    """
    from music_scan.identity import ItemIndex  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415

    logger = get_run_logger()
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
    with MusicLibrary(scan.LIBRARY_DB) as lib:
        index = ItemIndex(lib.all_items())
        result = ingest.sync_playlists(
            remove_sources, metrics, in_library=lambda pl, song: scan.link_song(index, pl, song)
        )
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
    if metrics.tracks_linked:
        logger.info("  %d track(s) already in the library, tagged instead of downloaded", metrics.tracks_linked)
    if metrics.playlists_skipped:
        logger.info("  %d nosync playlist(s) skipped", metrics.playlists_skipped)
    if metrics.playlists_deferred:
        logger.info("  %d playlist(s) deferred (budget/timeout)", metrics.playlists_deferred)
    return result


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
    logger.info("Cleared the source tag on %d beets item(s)", count)
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


@task(name="canon-albums", log_prints=True)
def canon_task(since: float) -> None:
    """Canonical album tags from Spotify on new and still-pending items (#209)."""
    logger = get_run_logger()
    from music_scan import canon  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    try:
        with MusicLibrary(scan.LIBRARY_DB) as lib:
            canon.after_scan(lib, since)
    except Exception:
        logger.exception("Canonical albums failed; the next scan or music-canon-albums retries it")


@task(name="beet-update", log_prints=True)
def beet_update_task() -> None:
    """Refresh beets library metadata."""
    logger = get_run_logger()
    from music_scan.process import run_beet_update  # noqa: PLC0415
    run_beet_update()
    logger.info("Beets library metadata refreshed")


@task(name="regen-playlists", log_prints=True)
def regen_playlists_task(metrics: ScanMetrics | None = None) -> None:
    """Regenerate .m3u playlist files from the beets library; *metrics* takes
    the empty-slot count per playlist (#228)."""
    logger = get_run_logger()
    counts = scan.regen_playlists(metrics) or {}
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
        since = time.time()
        imported = beet_import_task()
        metrics.quarantined_tracks = quarantine_task()
        metrics.tracks_imported = len(imported) + asis_import_task()
        canon_task(since)
        beet_update_task()
        regen_playlists_task(metrics)
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


# How long a flow that skips when busy waits for its lock. Not 0: Prefect wraps
# the whole acquire request in this timeout, so 0 times out before the server
# answers, even on a free limit (#174).
SKIP_IF_BUSY_SECONDS = 5


# Flow names are prefixed because the document-pipeline service serves its
# own flows against the same Prefect server. Prefect keys a deployment on
# flow name + deployment name, so a bare "scan" resolved to one shared
# record that both runners polled and each crashed on the other's
# entrypoint. "fetch" is prefixed too so it cannot repeat that.
@flow(name="music-fetch", log_prints=True)
def fetch_and_scan_flow() -> None:
    """Fetch: spotdl sync, then scan inbox.

    The fetch tasks share one ``ingest_run`` with ``ingest.run()``, so the music_ingest
    push (cookie expiry included) is the same in both paths (#203).
    """
    with concurrency("pipeline", occupy=1):
        with ingest.ingest_run() as metrics:
            preflight_task(metrics)
            remove_sources = reconcile_playlists_task(metrics)
            pending = spotdl_sync_task(remove_sources, metrics)
        save_removals_task(pending)
        _run_scan_tasks()


@flow(name="music-scan", log_prints=True)
def scan_flow() -> None:
    """Scan: apply any pending removals, import inbox, regenerate playlists."""
    logger = get_run_logger()
    try:
        with concurrency("pipeline", occupy=1, timeout_seconds=SKIP_IF_BUSY_SECONDS):
            _run_scan_tasks()
    except TimeoutError:
        logger.info("Scan skipped — pipeline busy (fetch or scan already running)")


# ---------------------------------------------------------------------------
# Album mode (#168)
# ---------------------------------------------------------------------------


def _album_library_hooks(lib):
    """have / missing / complete callbacks over one open beets library.

    ``have`` caches each playlist's track keys and the whole library for a tick;
    ``complete`` imports a finished download and drops the caches, since the
    import changed the library.  ``have`` also counts tracks the library holds
    under another playlist, and tags them with this one (#187).  ``missing``
    lists the tracks ``have`` would not find, tagging nothing (#205).
    """
    import music_fetch.albums as albums  # noqa: PLC0415
    from music_scan.identity import ItemIndex  # noqa: PLC0415

    keys: dict[str | None, ItemIndex] = {}  # None: the whole library

    def indexes(playlist: str) -> tuple[ItemIndex, ItemIndex]:
        if playlist not in keys:
            keys[playlist] = ItemIndex(lib.items_by_source(playlist))
        if None not in keys:
            keys[None] = ItemIndex(lib.all_items())
        return keys[playlist], keys[None]

    def have(playlist: str, tracks: list[list]) -> bool:
        return scan.have_or_link(*indexes(playlist), playlist, tracks)

    def missing(playlist: str, tracks: list[list]) -> list[list]:
        source, library = indexes(playlist)
        return scan.missing_tracks(source, tracks, library)

    def fresh_missing(playlist: str, tracks: list[list]) -> list[list]:
        return scan.missing_tracks(ItemIndex(lib.items_by_source(playlist)), tracks)

    def import_inbox() -> None:
        # Waits for a running fetch or scan: beets' SQLite has one writer.
        with concurrency("pipeline", occupy=1):
            _run_scan_tasks()

    def add_source(have_source: str, new_source: str, tracks: list[list[str]]) -> None:
        scan.add_source(lib, have_source, new_source, tracks)

    def tag_ids(playlist: str, tracks: list[list], since: float, tracks_count: int) -> None:
        scan.tag_album_ids(lib, playlist, tracks, since, tracks_count)
        canon_albums(playlist, since)
        embed_covers(playlist, since)

    def canon_albums(playlist: str, since: float) -> None:
        # Needs the Spotify IDs tag_album_ids just set (#209); before embed_covers,
        # which then only covers what the canonical album had no art for.
        from music_scan import canon  # noqa: PLC0415

        fresh = [i for i in lib.items_by_source(playlist) if (i.added or 0) >= since]
        try:
            canon.canonicalize_items(fresh)
        except Exception:
            canon.logger.exception("Canonical albums failed; the next scan or music-canon-albums retries it")

    def embed_covers(playlist: str, since: float) -> None:
        # Needs the Spotify IDs tag_album_ids just set (#204).  The import's
        # Navidrome rescan ran before this, so ask for another.
        from music_scan import cover  # noqa: PLC0415
        from music_scan.navidrome import trigger_scan  # noqa: PLC0415

        fresh = [i for i in lib.items_by_source(playlist) if (i.added or 0) >= since]
        try:
            if cover.embed_covers(fresh, cover.covers_by_id(ingest.SPOTDL_DIR)):
                trigger_scan()
        except Exception:
            cover.logger.exception("Album covers: embedding failed; music-embed-covers retries it")

    def rollback(playlist: str, since: float) -> int:
        # A blocklisted release's tracks on no playlist entry (#238); the
        # .m3u tails listed them, so regenerate.
        from music_scan import rollback as rb  # noqa: PLC0415

        try:
            with concurrency("pipeline", occupy=1):
                n = rb.rollback_release(lib, playlist, since, ingest.SPOTDL_DIR)
                if n:
                    scan.regen_playlists()
        except Exception:
            rb.logger.exception("Rollback failed; the release is still blocklisted, music-rollback-releases cleans up")
            return 0
        return n

    def adopt_extras(state, completion) -> None:
        # A successful release's extras take its album's tags, cover and folder (#245),
        # or its track's Spotify ID when they are one (#250); the .m3u files list them, so regenerate.
        from music_scan import canon  # noqa: PLC0415
        from music_scan.navidrome import trigger_scan  # noqa: PLC0415

        found = albums.find_by_nzo(state, completion.nzo_id)
        if found is None:
            return
        try:
            with concurrency("pipeline", occupy=1):
                extras, links = canon.extras_after_import(lib, *found)
                if links or any(e.dest != e.source for e in extras):
                    scan.regen_playlists()
            if links or any(e.write for e in extras):
                trigger_scan()
        except Exception:
            canon.logger.exception("Release extras failed; music-canon-albums retries them")

    def complete(state, completion) -> str | None:
        status = albums.complete(state, completion, import_inbox, fresh_missing, add_source, tag_ids, rollback)
        if status in (albums.IMPORTED, albums.FALLBACK):
            adopt_extras(state, completion)
        keys.clear()
        return status

    return have, missing, complete


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
        have, missing, complete = _album_library_hooks(lib)
        result = albums.tick(
            playlists,
            SpotifyPlaylists(ingest.COOKIE_FILE),
            Prowlarr(),
            Sabnzbd(),
            have,
            ingest.SPOTDL_DIR,
            settings,
            on_completion=complete,
            missing=missing,
        )

    removed: list = []
    for name, songs in result.removed_songs.items():
        for song in songs:
            ingest.schedule_unlink(removed, song, name)
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
        with concurrency("albums", occupy=1, timeout_seconds=SKIP_IF_BUSY_SECONDS):
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
                have, _, complete = _album_library_hooks(lib)
                status = complete(state, albums.Completion(nzo_id=nzo_id, ok=ok, path=path, fail_message=fail_message))
                if status is not None and settings.mode == "on":
                    albums.top_up(state, settings, Prowlarr(), Sabnzbd(), have, albums.TickResult())
            success = True
        finally:
            state.save()
            albums.push_metrics(state, success)
    logger.info("Album import for %s: %s", nzo_id, status or "not an album-mode job")

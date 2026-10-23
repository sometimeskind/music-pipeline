"""Prefect tasks and flows for the music pipeline."""

from __future__ import annotations

import json
import logging
import time

from prefect import flow, get_run_logger, task
from prefect.concurrency.sync import concurrency

import music_fetch.ingest as ingest
import music_scan.scan as scan
from music_fetch.config import load_playlists
from music_scan.metrics import ScanMetrics
from music_scan.scan import MusicBrainzUnavailable

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
def reconcile_playlists_task(metrics) -> None:
    """Reconcile playlists.conf: provision new entries, delete removed ones."""
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
        removed = ingest.reconcile_playlists()
    except Exception:
        metrics.failure_reason = "reconcile_error"
        raise
    if removed:
        logger.info("Removed from config (store and playlist deleted): %s", ", ".join(removed))
    else:
        logger.info("No playlists removed from config")


@task(name="spotdl-sync", log_prints=True, persist_result=False)
def spotdl_sync_task(metrics) -> None:
    """Run spotdl sync for all active playlists, writing their stores (homelab#1914).

    Tracks the library already holds are tagged with the playlist instead of
    downloaded (#187); see :func:`music_scan.scan.sync_hooks`.
    """
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
    with scan.sync_hooks() as hooks:
        ingest.sync_playlists(metrics, **hooks)
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
        "Sync complete: %d of %d track(s) downloaded%s, %d playlist(s) processed",
        metrics.tracks_downloaded,
        metrics.tracks_attempted,
        suffix,
        metrics.playlists_total,
    )
    if metrics.tracks_linked:
        logger.info("  %d track(s) already in the library, tagged instead of downloaded", metrics.tracks_linked)
    if metrics.playlists_skipped:
        logger.info("  %d nosync playlist(s) skipped", metrics.playlists_skipped)
    if metrics.playlists_deferred:
        logger.info("  %d playlist(s) deferred (budget/timeout)", metrics.playlists_deferred)


# ---------------------------------------------------------------------------
# Scan tasks
# ---------------------------------------------------------------------------


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


@task(name="retire-held-extras", log_prints=True)
def retire_held_task() -> None:
    """Retire the local-only extras whose recording their album's release now
    holds (#294), or merge them into the release item with the same audio
    (#301).  Before canon, so no extra still holds a path canon moves a
    release item onto (#283); the scan's playlist regeneration follows."""
    logger = get_run_logger()
    from music_scan import release  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    try:
        with MusicLibrary(scan.LIBRARY_DB) as lib:
            n = release.retire_held(lib)
    except Exception:
        logger.exception("Retiring held extras failed; the next scan retries it")
        return
    if n:
        logger.info("Retired %d extra(s) their album's release now holds → quarantine/replaced/", n)


@task(name="purge-replaced", log_prints=True)
def purge_replaced_task(metrics: ScanMetrics) -> None:
    """Delete superseded files from quarantine/replaced/ after their retention
    (#288); count what is left there by kind.  Runs under the scan's
    ``pipeline`` lock, so no import or retire moves files meanwhile."""
    logger = get_run_logger()
    from music_scan import release  # noqa: PLC0415
    from music_scan.audit import REPLACED  # noqa: PLC0415
    from music_scan.identity import Recordings  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    try:
        with MusicLibrary(scan.LIBRARY_DB) as lib:
            library = Recordings(lib.all_items())
        counts = release.purge_replaced(REPLACED, library, release.retention_days())
    except Exception:
        logger.exception("Purging quarantine/replaced/ failed; the next scan retries it")
        return
    metrics.replaced_purged = counts.pop("purged")
    metrics.replaced = counts


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


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def _run_scan_tasks() -> None:
    """Every scan pushes the music_scan job, failed or not, so its alerts see it."""
    metrics = ScanMetrics()
    start = time.monotonic()
    try:
        since = time.time()
        outage: MusicBrainzUnavailable | None = None
        try:
            imported = beet_import_task()
        except MusicBrainzUnavailable as exc:
            # The rest of the scan still runs for what did import; the
            # skipped files stay in the inbox for the next one (#264).
            outage, imported = exc, exc.imported
        if outage is None:
            metrics.quarantined_tracks = quarantine_task()
        metrics.tracks_imported = len(imported) + asis_import_task()
        retire_held_task()
        canon_task(since)
        beet_update_task()
        regen_playlists_task(metrics)
        purge_replaced_task(metrics)
        try:
            navidrome_task()
        except Exception:
            metrics.failure_reason = "navidrome_trigger_failed"
            raise
        if outage is not None:
            metrics.failure_reason = "musicbrainz_unavailable"
            raise outage
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
            reconcile_playlists_task(metrics)
            spotdl_sync_task(metrics)
        _run_scan_tasks()


@flow(name="music-scan", log_prints=True)
def scan_flow() -> None:
    """Scan: import inbox, regenerate playlists."""
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

    ``have`` caches the whole library for a tick; ``complete`` imports a
    finished download and drops the cache, since the import changed the
    library.  ``have`` is true when the library holds the album whole (#268),
    each track resolved by Spotify ID or ISRC as the store view resolves it
    (homelab#1914: ``sources`` is not membership); it tags the tracks with the
    playlist (#187).  ``missing`` lists the tracks the library lacks, tagging
    nothing (#205).
    """
    import music_fetch.albums as albums  # noqa: PLC0415
    from music_scan.identity import ItemIndex  # noqa: PLC0415

    from music_scan.playlists import by_album_id  # noqa: PLC0415

    keys: dict[str | None, ItemIndex] = {}  # None: the whole library
    albums_by_id: dict[str, list] = {}
    none = ItemIndex([])

    def indexes(playlist: str) -> tuple[ItemIndex, ItemIndex]:
        # No playlist index: an entry resolves to any item by ID or ISRC.
        if None not in keys:
            keys[None] = ItemIndex(lib.all_items())
        return none, keys[None]

    def have(key: str, record: dict) -> bool:
        library = indexes(next(iter(record.get("playlists", {})), ""))[1]
        if not albums_by_id:
            albums_by_id.update(by_album_id(library.items))
        if not scan.have_album(lambda pl: indexes(pl)[0], library, albums_by_id, key, record):
            return False
        for playlist, tracks in record["playlists"].items():
            scan.have_or_link(*indexes(playlist), playlist, tracks)
        return True

    def missing(playlist: str, tracks: list[list]) -> list[list]:
        source, library = indexes(playlist)
        return scan.missing_tracks(source, tracks, library)

    def import_inbox() -> None:
        # Waits for a running fetch or scan: beets' SQLite has one writer.
        with concurrency("pipeline", occupy=1):
            try:
                _run_scan_tasks()
            except MusicBrainzUnavailable as exc:
                # Not the release's fault: retried without blame (#264).
                raise albums.ImportDeferred(str(exc)) from exc

    def add_source(have_source: str, new_source: str, tracks: list[list[str]]) -> None:
        scan.add_source(lib, have_source, new_source, tracks)

    def import_rest(playlist: str, job: str) -> int:
        # The files the singleton import couldn't match are the album's too (#279).
        from music_scan import release  # noqa: PLC0415

        with concurrency("pipeline", occupy=1):
            return release.import_rest(scan.QUARANTINE, scan.INBOX, playlist, job)

    def claim(playlist: str, since: float, key: str, guid: str, job: str) -> list[str]:
        from music_scan import release  # noqa: PLC0415

        with concurrency("pipeline", occupy=1):
            return [i.title or "" for i in release.claim(lib.all_items(), playlist, since, key, guid, job)]

    def retire(key: str, record: dict, keep: str | None, drop: str | None) -> int:
        # Another copy of the album, or this one when it is another album (#279);
        # the .m3u files listed them, so regenerate.
        from music_scan import release  # noqa: PLC0415

        with concurrency("pipeline", occupy=1):
            n = release.retire(lib, key, record, keep, drop, scan.store_listings())
            if n:
                scan.regen_playlists()
        return n

    def identify(folder) -> dict | None:
        from music_scan import release  # noqa: PLC0415

        return release.identify(folder)

    def tag_ids(playlist: str, tracks: list[list], since: float, tracks_count: int) -> None:
        scan.tag_album_ids(lib, playlist, tracks, since, tracks_count)
        retire_held()
        canon_albums(playlist, since)
        supersede(playlist, since, tracks_count, tracks)
        embed_covers(playlist, since)

    def retire_held() -> None:
        # After tag_album_ids (#294): the release's new Spotify IDs may show it
        # holds an extra's recording.  Before canon, so the extra no longer
        # holds a path canon moves a release item onto (#283).
        from music_scan import release  # noqa: PLC0415

        try:
            with concurrency("pipeline", occupy=1):
                if release.retire_held(lib):
                    scan.regen_playlists()
        except Exception:
            release.logger.exception("Retiring held extras failed; the next scan retries it")

    def canon_albums(playlist: str, since: float) -> None:
        # Needs the Spotify IDs tag_album_ids just set (#209); before embed_covers,
        # which then only covers what the canonical album had no art for.
        from music_scan import canon  # noqa: PLC0415

        fresh = [i for i in lib.items_by_source(playlist) if (i.added or 0) >= since]
        try:
            canon.canonicalize_items(fresh)
        except Exception:
            canon.logger.exception("Canonical albums failed; the next scan or music-canon-albums retries it")

    def supersede(playlist: str, since: float, tracks_count: int, tracks: list[list]) -> None:
        # After canon, which files the loose items' slots (#266).  The .m3u
        # files listed the loose items, so regenerate.
        from music_scan import supersede as sup  # noqa: PLC0415
        from music_scan.navidrome import trigger_scan  # noqa: PLC0415

        try:
            with concurrency("pipeline", occupy=1):
                fresh = [i for i in lib.items_by_source(playlist)
                         if i.get("via") == "usenet" and (i.added or 0) >= since]
                n = sup.after_import(lib, fresh, tracks_count, tracks)
                if n:
                    scan.regen_playlists()
            if n:
                trigger_scan()
        except Exception:
            sup.logger.exception("Superseding loose copies failed; music-canon-albums retries it")

    def embed_covers(playlist: str, since: float) -> None:
        # Needs the Spotify IDs tag_album_ids just set (#204).  The import's
        # Navidrome rescan ran before this, so ask for another.
        from music_scan import cover  # noqa: PLC0415
        from music_scan.navidrome import trigger_scan  # noqa: PLC0415

        fresh = [i for i in lib.items_by_source(playlist) if (i.added or 0) >= since]
        try:
            if cover.embed_covers(fresh, cover.covers_by_id()):
                trigger_scan()
        except Exception:
            cover.logger.exception("Album covers: embedding failed; music-embed-covers retries it")

    def complete(state, completion) -> str | None:
        status = albums.complete(state, completion, import_inbox, claim, add_source, tag_ids, retire,
                                 identify=identify, import_rest=import_rest)
        keys.clear()
        albums_by_id.clear()
        return status

    return have, missing, complete


def _album_mixed_hook(lib):
    """``mixed(key, record)`` over one open beets library: why the record's
    album is mixed from several releases (``release.mixed_reasons``, #288).
    Reads the library once, when first called."""
    from music_scan import release  # noqa: PLC0415
    from music_scan.playlists import by_album_id  # noqa: PLC0415

    on_album: dict[str, list] = {}
    listed: list = []

    def mixed(key: str, record: dict) -> list[str]:
        if not on_album:
            on_album.update(by_album_id(lib.all_items()) or {"": []})
            listed.append(scan.store_listings())
        return release.mixed_reasons(key, record, on_album.get(key, []), listed[0])

    return mixed


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
        albums.tick(
            playlists,
            SpotifyPlaylists(ingest.COOKIE_FILE),
            Prowlarr(),
            Sabnzbd(),
            have,
            settings,
            on_completion=complete,
            missing=missing,
            mixed=_album_mixed_hook(lib),
        )


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

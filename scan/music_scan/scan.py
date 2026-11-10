"""music-scan: import inbox → beets, refresh metadata, regenerate .m3u playlists,
push Prometheus metrics.

Called frequently (every 5 min by default) and also after music-ingest completes.
No Spotify or YouTube calls.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import psycopg

from music_fetch import events, later_queue, state, store
from music_fetch.album_requests import UNVERIFIED
from music_scan import playlists
from music_scan.guard import REASONS, recheck_unknown, verify_requests
from music_scan.held import album_held, held_urls
from music_scan.identity import (
    BY_WORDS, HELD_ELSEWHERE, NO_ITEM, OTHER_TITLE, ItemIndex, PlaylistTrack, add_to_list, disc_track, item_isrcs,
    item_spotify_ids, number_match, same_base_title, skip_note, spotify_id,
)
from music_scan.identity import name_words as _name_words
from music_scan.library import MusicLibrary
from music_scan.metrics import ScanMetrics
from music_scan.navidrome import trigger_scan
from music_scan.playlists import Entry, Slot
from music_scan.process import ImportInterrupted, run_beet_import, run_beet_update, shutdown_requested

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}
SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
QUARANTINE = Path("/root/Music/quarantine")
# Album request fallback downloads the AcoustID check rejected (homelab#2546),
# and any the dropped length guard left (#165); never staged for the asis pass.
REJECTED = QUARANTINE / "rejected"
PLAYLISTS = Path("/root/Music/playlists")
INBOX = Path("/root/Music/inbox")
LIBRARY = Path("/root/Music/library")
LIBRARY_DB = Path("/root/.config/beets/library.db")
# The last scan's empty playlist slots are the state's empty-slots document: the
# Music MCP's digest_summary reports them (homelab#2413), the first of each listed.
EMPTY_SLOTS_LISTED = 25
# The scan's failure reason when a shutdown cut it short (homelab#2763).
INTERRUPTED = "interrupted"


def _snapshot_inbox(inbox: Path) -> list[Path]:
    """Return the sorted audio files currently in the inbox tree."""
    return sorted(
        f for f in inbox.rglob("*")
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )


def inbox_audio_count() -> int:
    """The audio files waiting in the inbox, counted as the import's snapshot
    finds them (homelab#2751): what the scan ignores doesn't count, such as a
    file still copying under its temporary name (rclone's ``.partial``), a cover
    or an nfo."""
    return len(_snapshot_inbox(INBOX)) if INBOX.is_dir() else 0


def _check_import_names(inbox_stems: list[str], imported: list[tuple[str, str]]) -> None:
    """Compare imported track names against the pre-import inbox snapshot.

    Flags tracks whose title+artist shares less than 40% Jaccard word-overlap
    with the closest matching inbox filename — a signal that beets may have
    applied a wildly wrong match.
    """
    if not inbox_stems or not imported:
        return

    inbox_word_sets = [_name_words(s) for s in inbox_stems]

    flagged = []
    for title, artist in imported:
        lib_words = _name_words(f"{title} {artist}")
        if not lib_words:
            continue
        best = max(
            (len(lib_words & iws) / max(len(lib_words | iws), 1) for iws in inbox_word_sets),
            default=0.0,
        )
        if best < 0.4:
            flagged.append((title, artist, best))

    if flagged:
        logger.warning("==> %d imported track(s) look unlike anything in the inbox (possible bad match):", len(flagged))
        for title, artist, score in sorted(flagged, key=lambda x: x[2]):
            logger.warning("  !! %s — %s  (best inbox overlap: %.0f%%)", title, artist, score * 100)
    else:
        logger.info("==> Name check OK: all %d imported tracks resemble their inbox source files", len(imported))


class MusicBrainzUnavailable(RuntimeError):
    """beets could not reach MusicBrainz during the import (#264).

    The tracks it skipped for that reason are no worse than before, so they stay
    in the inbox for the next scan instead of going to quarantine.  *imported*
    holds what the import did bring in, for the rest of the scan."""

    def __init__(self, message: str, imported: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.imported = imported or []


def run_inbox_import(skip_limit: int | None = None, folder: Path | None = None) -> list[tuple[str, str]]:
    """Snapshot inbox, run beet import, check names. Returns (title, artist) pairs.

    *folder*, a folder inside the inbox, confines the import to it: an album
    import's own release folder (homelab#2751).  The scan imports the whole inbox.

    Raises MusicBrainzUnavailable, after the import, when beets logged that
    MusicBrainz was unreachable and left inbox files behind: what it imported
    stays imported, the rest stays in the inbox.  When it left none (beets
    runs several searches per singleton, so one 503 rarely costs a match), the
    errors are only a warning: there is nothing to retry (music-pipeline#286).

    An import cut short raises too (homelab#2763): ``ImportInterrupted`` when
    the process is shutting down, ``CalledProcessError`` when beets exited
    non-zero or was killed by a signal.  The files beets didn't reach stay in
    the inbox, for the scan after the restart (homelab#2751) or the next one:
    a caller never quarantines them."""
    if skip_limit is None:
        skip_limit_env = os.environ.get("BEET_SKIP_LIMIT")
        skip_limit = int(skip_limit_env) if skip_limit_env else None
    if skip_limit is not None:
        logger.info("Skip limit    : %d (early termination enabled)", skip_limit)
    root = INBOX if folder is None else folder
    inbox_snapshot = _snapshot_inbox(root)
    logger.info("Inbox snapshot : %d audio file(s) queued for import", len(inbox_snapshot))
    import_start = time.time()
    try:
        mb_errors = run_beet_import(root, skip_limit=skip_limit)
    except (ImportInterrupted, subprocess.CalledProcessError):
        left = sum(1 for f in inbox_snapshot if f.exists())
        logger.warning("[INTERRUPTED] beet import cut short: %d of %d inbox file(s) stay in the inbox for the "
                       "next scan, none quarantined", left, len(inbox_snapshot))
        raise
    with MusicLibrary(LIBRARY_DB) as lib:
        imported = lib.items_added_since(import_start)
    logger.info("Newly imported : %d track(s)", len(imported))
    if mb_errors:
        left = sum(1 for f in inbox_snapshot if f.exists())
        if left:
            raise MusicBrainzUnavailable(
                f"beets could not reach MusicBrainz ({mb_errors} error line(s)); "
                f"{len(imported)} track(s) imported, {left} of {len(inbox_snapshot)} inbox file(s) "
                "stay in the inbox",
                imported,
            )
        logger.warning(
            "beets logged %d MusicBrainz error line(s), but left none of the %d inbox file(s) behind",
            mb_errors, len(inbox_snapshot),
        )
    if inbox_snapshot and not imported:
        logger.warning(
            "==> 0 of %d inbox file(s) were imported — beets skipped all tracks. "
            "Check ~/.config/beets/import.log for skip details.",
            len(inbox_snapshot),
        )
    _check_import_names([f.stem for f in inbox_snapshot], imported)
    return imported


def _count_quarantine() -> int:
    if not QUARANTINE.exists():
        return 0
    return sum(1 for f in QUARANTINE.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS)


def count_lossless_items() -> int | None:
    """Lossless items in the library, or None if the library can't be read.

    Feeds the FLAC guard: the count is pushed even when the scan fails."""
    try:
        with MusicLibrary(LIBRARY_DB) as lib:
            return lib.lossless_item_count()
    except Exception:
        logger.warning("Could not count lossless library items", exc_info=True)
        return None


def quarantine_inbox_leftovers(folder: Path | None = None) -> int:
    """Move any audio files still anywhere in the inbox tree to quarantine.

    After ``beet import`` has processed everything it can, un-matched audio
    files remain in the inbox.  We move them to quarantine for manual review,
    preserving the relative path so it's clear which playlist/album they came
    from.  *folder*, a folder inside the inbox, confines the move to it, as
    :func:`run_inbox_import` confined the import (homelab#2751).  Returns the
    count of files moved.

    Never while the process is shutting down (homelab#2763): the files are
    then the ones beets never reached, not the ones it couldn't match, and
    it raises ``ImportInterrupted`` instead.
    """
    if shutdown_requested():
        raise ImportInterrupted("the service is shutting down; the inbox is not quarantined")
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in (INBOX if folder is None else folder).rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            dest = QUARANTINE / f.relative_to(INBOX)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dest)
            moved += 1
    return moved


_ASIS_REQUIRED_TAGS = ("title", "artist", "album", "tracknumber")
_ASIS_SUBTREES = ("spotdl",)
# The music_pipeline plugin's duplicate hook doesn't run under --asis (beets
# sends no import_task_choice there), so beets would apply the main config's
# duplicate_action: remove and delete the existing item and its file (#202).
# A duplicate stays in quarantine instead, unless it is another recording
# (music_scan.asis_apart, homelab#2698).
_ASIS_CONFIG = "import:\n  duplicate_action: skip\n"
# Where the as-is pass stages its files, beside quarantine/ on its volume
# (homelab#2763): moving files there is a rename, and a pod killed mid-pass
# leaves them on the volume, for restore_asis_staging() at the next start.
# They were in tempfile.TemporaryDirectory()s in the container's /tmp, and 68
# files went with a container.  Not inside quarantine/, which the purge, the
# audits and list_quarantine walk.  The beets plugin finds a staged file's
# playlist under these names (music_pipeline.ASIS_STAGING_ROOTS).
ASIS_STAGING_NAME = "asis-staging"
ASIS_APART_NAME = "asis-apart"


def asis_dirs() -> tuple[Path, Path]:
    """The as-is pass's staging dir and its apart dir (:mod:`music_scan.asis_apart`):
    ``asis-staging/`` and ``asis-apart/`` beside :data:`QUARANTINE`."""
    return QUARANTINE.parent / ASIS_STAGING_NAME, QUARANTINE.parent / ASIS_APART_NAME


def _move_asis_eligible(quarantine: Path, staging: Path) -> int:
    """Move the audio files in *quarantine* the as-is pass takes
    (:func:`music_scan.asis_apart.eligible`) to *staging*.

    Files missing title, artist, album, or tracknumber are left in quarantine.
    Returns the count of files moved.
    """
    from music_scan.asis_apart import eligible  # noqa: PLC0415

    moved = 0
    for f in list(eligible(quarantine)):
        dest = staging / f.relative_to(quarantine)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest)
        moved += 1
    return moved


def _free_path(dest: Path) -> Path:
    """*dest*, or when a file is there already, the first free
    ``<stem> (restored N)<suffix>`` beside it."""
    n = 1
    free = dest
    while free.exists():
        free = dest.with_name(f"{dest.stem} (restored{'' if n == 1 else f' {n}'}){dest.suffix}")
        n += 1
    return free


def _return_to_quarantine(staging: Path) -> tuple[int, int]:
    """Move the files beets left in *staging* back to their quarantine paths,
    and remove the emptied directories under it.  Returns ``(moved, kept)``:
    *kept* of them found a file at their quarantine path, and went beside it
    under a ``(restored)`` name instead of over it (homelab#2763).  Idempotent:
    a *staging* that is empty or missing moves nothing."""
    if not staging.is_dir():
        return 0, 0
    moved = kept = 0
    for remaining in sorted(staging.rglob("*")):
        if remaining.is_file() and remaining.suffix.lower() in AUDIO_EXTS:
            dest = QUARANTINE / remaining.relative_to(staging)
            dest.parent.mkdir(parents=True, exist_ok=True)
            free = _free_path(dest)
            kept += free != dest
            shutil.move(str(remaining), free)
            moved += 1
    for d in sorted((d for d in staging.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
        with suppress(OSError):
            d.rmdir()  # only when empty: anything else stays for a look
    return moved, kept


def restore_asis_staging() -> tuple[int, int]:
    """Move every file left in the as-is staging and apart dirs (:func:`asis_dirs`)
    back to its quarantine path (homelab#2763), as :func:`_return_to_quarantine`
    does.  The service calls it at start, before anything can scan; the as-is
    pass calls it first too.  Returns ``(moved, kept)`` over both dirs."""
    moved = kept = 0
    for d in asis_dirs():
        m, k = _return_to_quarantine(d)
        moved, kept = moved + m, kept + k
    return moved, kept


def import_asis_from_quarantine() -> int:
    """Import quarantine files that have sufficient existing tags (--asis).

    Moves eligible files to the staging dir (:func:`asis_dirs`), runs ``beet
    import --asis``, and returns any beet-skipped files back to quarantine,
    whatever the import did: a failure or a shutdown returns them too, and a
    kill that skips that leaves them on the volume for
    :func:`restore_asis_staging` (homelab#2763).  A file that is another
    recording than its artist-and-title duplicates is imported in a second
    run that keeps both (:mod:`music_scan.asis_apart`).  Never starts while
    the process is shutting down (``ImportInterrupted``).  Returns the count
    of tracks imported.
    """
    from music_scan import asis_apart  # noqa: PLC0415

    if shutdown_requested():
        raise ImportInterrupted("the service is shutting down; the as-is pass is not started")
    restored, kept = restore_asis_staging()
    if restored:
        logger.warning("[RESUME] %d as-is file(s) left staged went back to quarantine first (%d beside a file "
                       "of the same name)", restored, kept)
    asis_start = time.time()
    staging, apart = asis_dirs()
    staging.mkdir(parents=True, exist_ok=True)
    apart.mkdir(parents=True, exist_ok=True)
    try:
        staged = _move_asis_eligible(QUARANTINE, staging)
        logger.info("Asis eligible : %d file(s) with sufficient tags", staged)
        if staged:
            moved: list = []
            try:
                with MusicLibrary(LIBRARY_DB) as lib:
                    moved = asis_apart.move_apart(staging, apart, lib._lib)
            except Exception:
                logger.warning("Could not check the as-is files for other recordings", exc_info=True)
            logger.info("Asis apart    : %d file(s) another recording than their duplicates", len(moved))
            with tempfile.TemporaryDirectory(prefix="asis-config-") as config_dir:
                config = Path(config_dir) / "config.yaml"
                config.write_text(_ASIS_CONFIG)
                if staged > len(moved):
                    run_beet_import(staging, asis=True, config=config)
                keep_start = time.time()
                if moved:
                    keep = Path(config_dir) / "keep.yaml"
                    keep.write_text(asis_apart.KEEP_CONFIG)
                    run_beet_import(apart, asis=True, config=keep)
            if moved:
                try:
                    with MusicLibrary(LIBRARY_DB) as lib:
                        asis_apart.record_kept(lib._lib, moved, keep_start)
                except Exception:
                    logger.warning("Could not record the kept_apart events", exc_info=True)
    finally:
        _, kept = restore_asis_staging()
        if kept:
            logger.warning("Asis pass     : %d skipped file(s) went back to quarantine beside a file of the same "
                           "name", kept)
    with MusicLibrary(LIBRARY_DB) as lib:
        asis_imported = lib.items_added_since(asis_start)
    logger.info("Asis pass     : %d track(s) imported from quarantine", len(asis_imported))
    return len(asis_imported)


def _item_path(item) -> Path:
    raw = item.path.decode() if isinstance(item.path, bytes) else item.path
    return Path(raw) if Path(raw).is_absolute() else LIBRARY / raw


def playlist_paths(slots: list[Slot]) -> list[Path]:
    """The ``.m3u`` lines of a playlist: its resolved slots in playlist order,
    each file once."""
    ordered: list[Path] = []
    matched: set[Path] = set()
    for slot in slots:
        if slot.item is None:
            continue
        p = _item_path(slot.item)
        if p not in matched:
            ordered.append(p)
            matched.add(p)
    return ordered


def store_view(name: str, library: ItemIndex, by_album,
               stamps: dict[str, str] | None = None,
               playlist: store.Store | None = None) -> tuple[list[Path], list[Slot]] | None:
    """The ``.m3u`` paths playlist *name* gets from its store (homelab#1914),
    and the slots behind them: its entries resolved by Spotify ID or ISRC only,
    against the whole library, each file once and in store order; an ``album``
    playlist through :func:`playlists.album_paths` on the same entries.  Local
    entries are not in the view (:func:`_local_tail`).  None when its store
    can't be read: *name* comes from :func:`store.names`, so the file exists.
    *playlist*: the store, already loaded (the two-way sync reads it once)."""
    if playlist is None:
        playlist = store.load(name)
    if playlist is None:
        logger.error("  [STORE] %s: its store can't be read — its .m3u is left as it was", name)
        return None
    none = ItemIndex([])
    if (SPOTDL_DIR / f"{name}.album").exists():
        paths, slots, n_albums = playlists.album_paths(playlist.songs, none, library, by_album, stamps)
        logger.info("  %s: %d album(s), %d track(s) (album level)", name, n_albums, len(paths))
        return paths, slots
    slots = playlists.resolve([Entry.from_song(s) for s in playlist.songs], none, library)
    return playlist_paths(slots), slots


def _local_tail(name: str, paths: list[Path], library: ItemIndex) -> list[Path]:
    """The ``.m3u`` paths of playlist *name*'s local entries, after its store
    view *paths*: each local entry whose item is still in the library, in store
    order.  A local entry whose item the view now holds (it gained a Spotify
    ID) is dropped: its Spotify entry decides its place, and its removal.
    Logs one ``[STORE-TAIL]`` line."""
    playlist = store.load(name)
    if playlist is None:
        return []
    matched = set(paths)
    by_id = {item.id: item for item in library.items}

    def resolved(entry: dict) -> bool:
        local = entry.get(store.LOCAL)
        return bool(local) and local.get("id") in by_id and _item_path(by_id[local["id"]]) in matched

    dropped = [e for e in playlist.entries if resolved(e)]
    if dropped:
        playlist.entries = [e for e in playlist.entries if not resolved(e)]
        store.save(playlist, by="regen")
    message = (f"[STORE-TAIL] {name}: {len(playlist.locals)} local entr{'y' if len(playlist.locals) == 1 else 'ies'}"
               f", {len(dropped)} resolved")
    if dropped:
        logger.info("  %s", message)
        events.record("state_edited", message, source="scan",
                      data={"what": "store_tail", "playlist": name,
                            "items": [e[store.LOCAL].get("id") for e in dropped], "local": len(playlist.locals)})
    else:
        # Every playlist, every scan, mostly "0 local entries, 0 resolved" (~3,650 a day): nothing changed.
        logger.debug("  %s", message)
    out: list[Path] = []
    for entry in playlist.locals:
        if entry.get("id") in by_id and (p := _item_path(by_id[entry["id"]])) not in matched:
            matched.add(p)
            out.append(p)
    return out


def store_listings() -> Callable[[object], set[str]]:
    """``listed(item)``: the playlists whose store lists *item*'s recording,
    by Spotify ID or ISRC, or holds it as a local entry (homelab#1914).  By
    recording, not by the item an entry resolves to: a loose copy of a track
    another playlist lists is listed too."""
    by_key: dict[str, set[str]] = {}
    local: dict[object, set[str]] = {}
    for name in store.names():
        playlist = store.load(name)
        if playlist is None:
            continue
        for song in playlist.songs:
            if sid := song.get("song_id") or spotify_id(song.get("url")):
                by_key.setdefault(f"spotify:{sid}", set()).add(name)
            if code := song.get("isrc"):
                by_key.setdefault(f"isrc:{code}", set()).add(name)
        for entry in playlist.locals:
            local.setdefault(entry.get("id"), set()).add(name)

    def listed(item) -> set[str]:
        names = set(local.get(item.id, ()))
        for sid in item_spotify_ids(item):
            names |= by_key.get(f"spotify:{sid}", set())
        for code in item_isrcs(item):
            names |= by_key.get(f"isrc:{code}", set())
        return names

    return listed


@contextmanager
def sync_hooks() -> Iterator[dict]:
    """The nightly sync's view of the library (:func:`music_fetch.ingest.sync_playlists`):
    ``resolves`` (a library item holds the track by Spotify ID or ISRC, no
    tagging), ``held`` (downloads waiting in the inbox or quarantine, and the
    tracks of the albums album mode will grab next, #324) and
    ``in_library`` (the full ladder, tagging the item with the playlist, #187).
    The index is built once: a track downloaded earlier in the run isn't in
    the library until the scan, and is held instead."""
    with MusicLibrary(LIBRARY_DB) as lib:
        index = ItemIndex(lib.all_items())
        yield {
            "resolves": lambda song: index.match_song(song, words=False)[0] is not None,
            "held": held_urls(QUARANTINE, SPOTDL_DIR) | album_held(),
            "in_library": lambda pl, song: link_song(index, pl, song),
        }


def _album_records() -> dict[str, dict]:
    """The album records, for the stamps on their releases' items; none when unreadable."""
    from music_scan.canon import _records  # noqa: PLC0415

    return _records()


def regen_playlists(metrics: ScanMetrics | None = None) -> dict[str, int]:
    """Regenerate the .m3u file of every playlist with a store. Returns {name: track_count}.

    A playlist's ``.m3u`` is its store's view (:func:`store_view`), then its
    local entries (:func:`_local_tail`): each entry resolved to any library
    item by Spotify ID or ISRC, so a track imported by any route reaches every
    playlist that lists it at the next scan, with no download and no tag
    write (#228).  *metrics*, when given (the scan), takes the empty-slot
    count per playlist, and the state's ``empty-slots`` document the first
    :data:`EMPTY_SLOTS_LISTED` empty entries of each.

    An ``album`` playlist lists every local track of every album on it instead
    (#268, :func:`playlists.album_paths`).  A playlist whose store fails is
    logged and keeps its last ``.m3u``: a store failure never fails a scan or
    empties a playlist.

    ``later.m3u`` is the Later queue's available albums (homelab#2478,
    :func:`later_view`); a playlist store named ``later`` is not read for it.  When the queue can't be read,
    ``later.m3u`` keeps its last content.
    """
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    names = store.names()

    counts: dict[str, int] = {}
    empty: dict[str, int] = {}
    missing: dict[str, list[str]] = {}
    with MusicLibrary(LIBRARY_DB) as lib:
        all_items = lib.all_items()
        library = ItemIndex(all_items)
        by_album = playlists.by_album_id(all_items)
        by_record = playlists.by_stamp(all_items)
        stamps = playlists.record_stamps(_album_records())
        queue: list[Path] | None = None
        try:
            queue = later_view(library, by_album, by_record)
        except Exception:
            logger.error("  [LATER] the Later queue can't be read — %s.m3u is left as it was", later_queue.PLAYLIST,
                         exc_info=True)
        for name in names:
            if name == later_queue.PLAYLIST:
                continue  # the Later queue writes later.m3u
            try:
                view = store_view(name, library, by_album, stamps)
                if view is None:
                    continue
                paths, slots = view
                playlists.log_slots(name, slots)
                paths = paths + _local_tail(name, paths, library)
            except Exception:
                logger.error("Playlist store failed for %s — its .m3u is left as it was", name, exc_info=True)
                continue
            counts[name] = _write_m3u(name, paths)
            empty[name] = playlists.empty_slots(slots)
            missing[name] = [f"{s.entry.artist} — {s.entry.name}" for s in slots if s.item is None]
        if queue is not None and (queue or (PLAYLISTS / f"{later_queue.PLAYLIST}.m3u").exists()):
            counts[later_queue.PLAYLIST] = _write_m3u(later_queue.PLAYLIST, queue)

    if metrics is not None:
        metrics.slots_empty = empty
        _save_empty_slots(empty, missing)
    return counts


def _write_m3u(name: str, paths: list[Path]) -> int:
    lines = [os.path.relpath(p, PLAYLISTS) for p in paths]
    (PLAYLISTS / f"{name}.m3u").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def _later_albums() -> list[tuple[str, dict]]:
    """The Later queue's available albums, ``(key, record)`` in ``.m3u`` order,
    once the entries named by ref whose rows now have a release group have it
    (:func:`later_queue.resolve_refs`) and the pending albums the library now
    holds whole have become available (:func:`later_queue.mark_available`).
    Raises when the database can't be read."""
    with state.session() as conn:
        rows = state.load_albums(conn)
        later_queue.resolve_refs(conn, rows)
        later_queue.mark_available(conn, rows)
        return later_queue.albums(conn, rows)


def later_view(library: ItemIndex, by_album: dict[str, list], by_record: dict[str, list]) -> list[Path]:
    """The Later queue's ``.m3u`` paths (homelab#2478, :mod:`music_fetch.later_queue`):
    each available album's local tracks (:func:`album_tracks`), album by album
    in ``available_at`` order, each file once.  An import's album becomes
    available here (:func:`_later_albums`), so it joins the end at the scan
    that follows it.  Raises when the database can't be read."""
    found = _later_albums()
    paths = later_paths(found, library, by_album, by_record)
    if found:
        logger.info("  [LATER] %s: %d album(s), %d track(s) from the Later queue", later_queue.PLAYLIST, len(found),
                    len(paths))
    return paths


def later_paths(found: list[tuple[str, dict]], library: ItemIndex, by_album: dict[str, list],
                by_record: dict[str, list]) -> list[Path]:
    """The ``.m3u`` paths of the Later albums *found* (``(key, record)`` in
    ``.m3u`` order): each one's local tracks, album by album, each file once.
    ``music-cutover`` renders the queue it would write with it."""
    none = ItemIndex([])
    paths: list[Path] = []
    seen: set[Path] = set()
    for key, record in found:
        slots: list[Slot] = []
        for tracks in record.get("playlists", {}).values():
            slots += _album_slots(none, tracks, library)
        for item in album_tracks(key, record, slots, by_album, by_record):
            if (p := _item_path(item)) not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _save_empty_slots(empty: dict[str, int], missing: dict[str, list[str]]) -> None:
    """Best-effort: a playlist's slots are only ever reported, never acted on."""
    report = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "playlists": {name: {"total": n, "entries": missing.get(name, [])[:EMPTY_SLOTS_LISTED]}
                            for name, n in sorted(empty.items())}}
    try:
        state.save_doc(state.EMPTY_SLOTS, report)
    except (state.Unavailable, psycopg.Error) as exc:
        logger.warning("Could not save the empty slots: %s", exc)


def add_source(lib: MusicLibrary, have_source: str, new_source: str, tracks: list[list]) -> int:
    """Append *new_source* to the *have_source* items matching *tracks*, and the
    track's Spotify ID when an ID matched it.  Title words give the source only
    (#279, homelab#2664): :func:`tag_album_ids` runs for *new_source* next and
    decides the ID, so a track doesn't get one here that the first playlist's
    entries wouldn't.  Returns the count of items changed."""
    index = ItemIndex(lib.items_by_source(have_source))
    count = 0
    for track in map(PlaylistTrack.from_entry, tracks):
        item, rung = index.match_track(track)
        if item is not None and _link(item, new_source, None if rung == BY_WORDS else track.song_id):
            count += 1
    return count


def _link(item, source: str, song_id: str | None) -> bool:
    """Append *source* and *song_id* to *item* and store it. True when it changed."""
    changed = add_to_list(item, "sources", source)
    changed = add_to_list(item, "spotify_ids", song_id) or changed
    if changed:
        item.store()
    return changed


def link_song(index: ItemIndex, source: str, song: dict) -> bool:
    """Tag the library item holding a playlist *song* with *source* instead of downloading it (#187).

    Matches by Spotify ID, then ISRC, never title+artist: a wrong skip loses the
    track silently, while a redundant download is merged by the duplicate hook.
    True when the library has the song.
    """
    item, _ = index.match_song(song, words=False)
    if item is None:
        return False
    _link(item, source, song.get("song_id") or spotify_id(song.get("url")))
    return True


def _album_slots(source_index: ItemIndex, tracks: list[list], library: ItemIndex | None) -> list[Slot]:
    """The slots of an album record's per-playlist *tracks* (#228)."""
    entries = [Entry.from_track(PlaylistTrack.from_entry(t)) for t in tracks]
    return playlists.resolve(entries, source_index, library)


def missing_tracks(source_index: ItemIndex, tracks: list[list], library: ItemIndex | None = None) -> list[list]:
    """The entries of *tracks* no *source_index* item matches by the full ladder,
    nor, given *library*, any item by Spotify ID or ISRC (#205): the empty slots.
    Tags nothing."""
    return [entry for entry, slot in zip(tracks, _album_slots(source_index, tracks, library)) if slot.item is None]


def have_or_link(source_index: ItemIndex, library: ItemIndex, source: str, tracks: list[list]) -> bool:
    """True when the library holds every track, so the album needs no download (#187).

    A track counts when its slot resolves: to *source*'s items by the full
    ladder, or to any item by Spotify ID or ISRC.  Only when every track is
    present are the items found outside *source* tagged with it, so its .m3u
    lists them.
    """
    slots = _album_slots(source_index, tracks, library)
    if any(slot.item is None for slot in slots):
        return False
    for slot in slots:
        if slot.filled_from_library:
            _link(slot.item, source, slot.entry.song_id)
    return True


def have_album(indexes, library: ItemIndex, by_album: dict[str, list], key: str, record: dict,
               by_record: dict[str, list] | None = None) -> bool:
    """True when the library holds the album *record* whole (#268), so it needs no download.

    Every playlist track resolves (*indexes(playlist)*: the playlist's items,
    then any item by Spotify ID or ISRC), and the album's local tracks
    (:func:`album_tracks`) number at least Spotify's track count.  One track
    already in the library no longer makes an album "have": adding one song
    on Spotify brings in the whole album."""
    slots: list[Slot] = []
    for playlist, tracks in record.get("playlists", {}).items():
        slots += _album_slots(indexes(playlist), tracks, library)
    if not slots or any(slot.item is None for slot in slots):
        return False
    have = len(album_tracks(key, record, slots, by_album, by_record))
    return have >= (record.get("tracks_count") or 0)


def album_tracks(key: str, record: dict, slots: list[Slot], by_album: dict[str, list],
                 by_record: dict[str, list] | None = None) -> list:
    """The local tracks of album *record* (key *key*) whose tracks resolved
    to *slots* (:func:`playlists.album_items`): by its primary Spotify album,
    or by its key's ``album_record`` stamp alone when it has none (a
    request's, homelab#2478)."""
    return playlists.album_items(state.primary_spotify(record), record.get("name", ""), slots, by_album, key,
                                 by_record)


def _release_isrc_match(track: PlaylistTrack, items: list):
    """``(item, ambiguous)``: the one of *items* (this import's) carrying
    *track*'s ISRC.  When several do, the one whose title and then disc/track
    agree; when that leaves a tie, none (#278): beets matched two files to one
    recording (*Love* tagged *Bodies*), and only a listen tells which is which."""
    have = [i for i in items if track.isrc in item_isrcs(i)]

    def key(i):
        return (not same_base_title(track.name, i.title or ""), disc_track(i) != (track.disc, track.track))

    have.sort(key=key)
    if len(have) > 1 and key(have[0]) == key(have[1]):
        return None, True
    return (have[0] if have else None), False


def tag_album_ids(lib: MusicLibrary, source: str, tracks: list[list], since: float, tracks_count: int,
                  key: str | None = None, recordings=None) -> int:
    """Record each album-mode playlist track's Spotify ID on the *source* item it maps to.

    Usenet files carry no Spotify IDs (#176).  Per track, first hit wins:
    1. ISRC among the item's ISRCs: this import's usenet items first, the one
       whose title (edition notes dropped) and then disc/track agree when
       several carry it, none (``[AMBIG]``) when that leaves a tie (#278);
       else any of *source*'s items;
    2. disc and track number and title together, on usenet items imported
       since *since* (:func:`~music_scan.identity.number_match`, which says
       when they don't link): a number alone is not identity (#278), and a
       file beets matched to another recording (*Love* tagged *Bodies*) gets
       no ID and logs ``[NUMBER]``.  *recordings* looks up a recording when
       ISRCs disagree (a seam for the tests).  A link the release's own
       numbering doesn't back (a disc of 0, ``tracktotal`` not Spotify's
       count, homelab#2664) only fills an empty entry: when any library item
       holds the entry the release item stays as it is, because a shared ID
       is what moves a copy out of the library (:mod:`music_scan.supersede`,
       :class:`~music_scan.identity.Recordings`).  Its ID also goes in
       ``words_ids``, so it never supersedes.
    Title words alone never give an ID (#279): the IDs are a best effort, and an
    album record's membership never depends on them (``album_record``,
    :mod:`music_scan.release`).  The words rung of #240 is gone.
    Local-only extras (``album_extra``) are left out (#294): an older copy
    that already holds a track's ID or ISRC would take the match, and the
    release would never get the ID that retires the extra
    (``release.retire_held``).
    The IDs linked and the tracks left without one (``[AMBIG]``, ``[NUMBER]``,
    ``[NOID]``) are one ``id_linked`` event for the album record *key*
    (homelab#2590).  Returns the count of items tagged.
    """
    from music_scan.backfill import mb_recordings  # noqa: PLC0415
    from music_scan.release import EXTRA  # noqa: PLC0415

    index = ItemIndex(i for i in lib.items_by_source(source) if not i.get(EXTRA))
    imported = [i for i in index.items if i.get("via") == "usenet" and (i.added or 0) >= since]
    recordings = recordings or mb_recordings()
    library: ItemIndex | None = None
    linked: list[dict] = []
    skipped: list[dict] = []
    for track in map(PlaylistTrack.from_entry, tracks):
        if not track.song_id or track.song_id in index.by_id:
            continue
        match, ambiguous = _release_isrc_match(track, imported) if track.isrc else (None, False)
        if ambiguous:
            logger.warning("  [AMBIG] %s: %s — %s: several of this release's items carry ISRC %s and none is "
                           "told apart by title or disc/track; no ID", source, track.name, track.artist, track.isrc)
            skipped.append({"song_id": track.song_id, "title": track.name, "why": "ambiguous isrc",
                            "isrc": track.isrc})
            continue
        if match is None:
            match, _ = index.match(None, track.isrc, words=False)
        via = "isrc" if match is not None else None
        skip = None
        if match is None and track.disc and track.track:
            at, skip = number_match(track, imported, recordings)
            backed = at is not None and bool(tracks_count) and at.tracktotal == tracks_count and at.disc == track.disc
            if at is not None and not backed:
                library = ItemIndex(lib.all_items()) if library is None else library
                holder, _ = library.match(track.song_id, track.isrc, words=False)
                if holder is not None and holder.id != at.id:
                    at, skip = None, {"why": HELD_ELSEWHERE, "item_id": at.id, "held_by": holder.id,
                                      "disc": track.disc, "track": track.track}
            if at is not None:
                match, via = at, "number"
                if not backed:
                    add_to_list(match, "words_ids", track.song_id)
        if match is None:
            skip = skip or {"why": NO_ITEM}
            logger.info("  [%s] %s: %s — %s %s; no ID", "NUMBER" if skip["why"] == OTHER_TITLE else "NOID", source,
                        track.name, track.artist, skip_note(skip))
            skipped.append({"song_id": track.song_id, "title": track.name, **skip})
            continue
        add_to_list(match, "spotify_ids", track.song_id)
        match.store()
        linked.append({"item_id": match.id, "song_id": track.song_id, "via": via})
    if linked or skipped:
        message = (f"[IDS] {source}: {len(linked)} Spotify track ID(s) linked to this release's items"
                   + (f", {len(skipped)} left without one" if skipped else ""))
        logger.info("  %s", message)
        events.record("id_linked", message, source="scan", item_id=linked[0]["item_id"] if len(linked) == 1 else None,
                      data={"key": key, "playlist": source, "linked": linked, "skipped": skipped})
    return len(linked)



def check_request_downloads() -> Counter:
    """Check album requests' fallback downloads (homelab#2478), and re-check
    the held ones AcoustID didn't know (homelab#2546), before the import, so
    what AcoustID confirms imports in this scan.  A step of
    :data:`PRE_IMPORT_STEPS`.  Returns the new rejections per reason; a failure
    leaves the files for the next scan."""
    logger.info("==> Checking album requests' fallback downloads...")
    rejected: Counter = Counter()
    try:
        rejected.update(verify_requests(UNVERIFIED, SPOTDL_DIR, REJECTED, AUDIO_EXTS))
    except Exception:
        logger.error("Request fallback check failed — its downloads wait for the next scan", exc_info=True)
    try:
        recheck_unknown(REJECTED, SPOTDL_DIR)
    except Exception:
        logger.error("AcoustID re-check of held downloads failed — tried again at the next scan", exc_info=True)
    return rejected


# The checks every scan runs on the inbox before the import, in order, as
# (Prefect task name, function in this module).  :func:`run` and the Prefect
# scan flow (music_service.flows) both run exactly this list, so a check can't
# be wired into one path and not the other again: the length guard (#165) ran
# only in run() while production runs the flow, so it never ran there and was
# dropped (homelab#2579).  Each step returns the files it rejected per reason
# and contains its own failures.
PRE_IMPORT_STEPS: tuple[tuple[str, str], ...] = (
    ("verify-request-downloads", "check_request_downloads"),
)


def run() -> None:
    """Execute the full scan pipeline, push metrics on completion."""
    metrics = ScanMetrics()
    start = time.monotonic()

    try:
        logger.info("==> music-scan starting")

        quarantined_before = _count_quarantine()

        rejected: Counter = Counter()
        for _task, function in PRE_IMPORT_STEPS:
            rejected.update(globals()[function]())
        metrics.rejected = {**dict.fromkeys(REASONS, 0), **rejected}

        logger.info("==> Importing from inbox...")
        since = time.time()
        outage: MusicBrainzUnavailable | None = None
        try:
            imported = run_inbox_import()
        except MusicBrainzUnavailable as exc:
            # The rest of the scan still runs for what did import (#264).
            outage, imported = exc, exc.imported
            logger.warning("MusicBrainz unavailable: %s", exc)

        if outage is None:
            logger.info("==> Quarantining skipped files...")
            moved = quarantine_inbox_leftovers()
            logger.info("Quarantined : %d file(s) → %s", moved, QUARANTINE)
            logger.info("Log         : ~/.config/beets/import.log")

        logger.info("==> Importing quarantine with existing tags (--asis)...")
        asis_count = 0
        try:
            asis_count = import_asis_from_quarantine()
        except Exception:
            logger.error("Asis-import step failed — continuing with library update", exc_info=True)
        metrics.tracks_imported = len(imported) + asis_count

        logger.info("==> Canonical album tags from Spotify...")
        try:
            from music_scan import canon  # noqa: PLC0415

            with MusicLibrary(LIBRARY_DB) as lib:
                canon.after_scan(lib, since)
        except Exception:
            logger.error("Canonical-album step failed — continuing with library update", exc_info=True)

        logger.info("==> Refreshing library metadata...")
        try:
            run_beet_update()
        except Exception:
            logger.error("Library-update step failed — continuing with playlist regeneration", exc_info=True)

        logger.info("==> Regenerating playlists...")
        try:
            regen_playlists(metrics)
        except Exception:
            logger.error("Playlist-regeneration step failed", exc_info=True)

        quarantined_after = _count_quarantine()
        metrics.quarantined_tracks = max(0, quarantined_after - quarantined_before)

        logger.info("==> music-scan import complete")

    except Exception:
        metrics.success = False
        metrics.failure_reason = "unexpected_error"
        logger.exception("music-scan failed")
        raise
    except BaseException:
        # A shutdown (ImportInterrupted, a signal, Ctrl-C) cut the scan short
        # (homelab#2763): what it didn't reach waits for the next one.
        metrics.success = False
        metrics.failure_reason = INTERRUPTED
        logger.warning("music-scan interrupted — what it didn't reach waits for the next scan")
        raise
    else:
        try:
            trigger_scan()
        except Exception as exc:
            metrics.success = False
            metrics.failure_reason = "navidrome_trigger_failed"
            logger.error("Navidrome rescan trigger failed — tracks may not appear in Navidrome: %s", exc)
            raise
        if outage is not None:
            metrics.success = False
            metrics.failure_reason = "musicbrainz_unavailable"
            raise outage
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.lossless_items = count_lossless_items()
        metrics.push()

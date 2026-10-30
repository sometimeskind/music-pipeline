"""music-scan: import inbox → beets, refresh metadata, regenerate .m3u playlists,
push Prometheus metrics.

Called frequently (every 5 min by default) and also after music-ingest completes.
No Spotify or YouTube calls.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from music_fetch import albums, later_queue, state, store
from music_scan import playlists
from music_scan.guard import guard_inbox
from music_scan.held import album_held, held_urls
from music_scan.identity import (
    ItemIndex, PlaylistTrack, add_to_list, item_isrcs, item_spotify_ids, same_base_title, spotify_id,
)
from music_scan.identity import name_words as _name_words
from music_scan.library import MusicLibrary
from music_scan.metrics import ScanMetrics
from music_scan.navidrome import trigger_scan
from music_scan.playlists import Entry, Slot
from music_scan.process import run_beet_import, run_beet_update

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}
SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
QUARANTINE = Path("/root/Music/quarantine")
# Files the length guard rejected (#165); never staged for the asis pass.
REJECTED = QUARANTINE / "rejected"
PLAYLISTS = Path("/root/Music/playlists")
INBOX = Path("/root/Music/inbox")
LIBRARY = Path("/root/Music/library")
LIBRARY_DB = Path("/root/.config/beets/library.db")
# The last scan's empty playlist slots, by name, beside the album store (SPOTDL_DIR):
# the Music MCP's digest_summary reports them (homelab#2413).
EMPTY_SLOTS_NAME = ".empty-slots.json"
EMPTY_SLOTS_LISTED = 25


def _snapshot_inbox(inbox: Path) -> list[str]:
    """Return sorted list of audio filename stems currently in the inbox tree."""
    return sorted(
        f.stem for f in inbox.rglob("*")
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )


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


def run_inbox_import(skip_limit: int | None = None) -> list[tuple[str, str]]:
    """Snapshot inbox, run beet import, check names. Returns (title, artist) pairs.

    Raises MusicBrainzUnavailable, after the import, when beets logged that
    MusicBrainz was unreachable: what it imported stays imported, the rest
    stays in the inbox."""
    if skip_limit is None:
        skip_limit_env = os.environ.get("BEET_SKIP_LIMIT")
        skip_limit = int(skip_limit_env) if skip_limit_env else None
    if skip_limit is not None:
        logger.info("Skip limit    : %d (early termination enabled)", skip_limit)
    inbox_snapshot = _snapshot_inbox(INBOX)
    logger.info("Inbox snapshot : %d audio file(s) queued for import", len(inbox_snapshot))
    import_start = time.time()
    mb_errors = run_beet_import(INBOX, skip_limit=skip_limit)
    with MusicLibrary(LIBRARY_DB) as lib:
        imported = lib.items_added_since(import_start)
    logger.info("Newly imported : %d track(s)", len(imported))
    if mb_errors:
        raise MusicBrainzUnavailable(
            f"beets could not reach MusicBrainz ({mb_errors} error line(s)); "
            f"{len(imported)} of {len(inbox_snapshot)} inbox file(s) imported, the rest stay in the inbox",
            imported,
        )
    if inbox_snapshot and not imported:
        logger.warning(
            "==> 0 of %d inbox file(s) were imported — beets skipped all tracks. "
            "Check ~/.config/beets/import.log for skip details.",
            len(inbox_snapshot),
        )
    _check_import_names(inbox_snapshot, imported)
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


def quarantine_inbox_leftovers() -> int:
    """Move any audio files still anywhere in the inbox tree to quarantine.

    After ``beet import`` has processed everything it can, un-matched audio
    files remain in the inbox.  We move them to quarantine for manual review,
    preserving the relative path so it's clear which playlist/album they came
    from.  Returns the count of files moved.
    """
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in INBOX.rglob("*"):
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
# A duplicate stays in quarantine instead.
_ASIS_CONFIG = "import:\n  duplicate_action: skip\n"


def _move_asis_eligible(quarantine: Path, staging: Path) -> int:
    """Move audio files from *quarantine* that have all required tags to *staging*.

    Only spotdl leftovers (``spotdl/<playlist>/``) and loose files at the
    quarantine root are eligible; every other subtree is skipped (#202).
    Files missing title, artist, album, or tracknumber are left in quarantine.
    Returns the count of files moved.
    """
    from mutagen import File as MutagenFile  # noqa: PLC0415 — beets dep, always available

    moved = 0
    for f in sorted(quarantine.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        # An allow-list, so a new quarantine subtree is skipped until it is
        # known to be safe.  Skipped: album-mode releases (usenet/: verified by
        # the strict match alone, a release beets can't match is blocklisted),
        # the length guard's rejects (rejected/, #165) and the files
        # music-audit-lengths --replace swapped out (replaced/, #202): both
        # carry the right tags on the wrong audio.
        parts = f.relative_to(quarantine).parts
        if len(parts) > 1 and parts[0] not in _ASIS_SUBTREES:
            continue
        try:
            tags = MutagenFile(f, easy=True)
            if tags is None or not all(tags.get(k) for k in _ASIS_REQUIRED_TAGS):
                continue
        except Exception:
            continue
        dest = staging / f.relative_to(quarantine)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest)
        moved += 1
    return moved


def import_asis_from_quarantine() -> int:
    """Import quarantine files that have sufficient existing tags (--asis).

    Moves eligible files to a temp staging dir, runs ``beet import --asis``,
    and returns any beet-skipped files back to quarantine.  Returns the count
    of tracks imported.
    """
    asis_start = time.time()
    with tempfile.TemporaryDirectory(prefix="asis-staging-") as staging_str:
        staging = Path(staging_str)
        staged = _move_asis_eligible(QUARANTINE, staging)
        logger.info("Asis eligible : %d file(s) with sufficient tags", staged)
        if staged:
            with tempfile.TemporaryDirectory(prefix="asis-config-") as config_dir:
                config = Path(config_dir) / "config.yaml"
                config.write_text(_ASIS_CONFIG)
                run_beet_import(staging, asis=True, config=config)
            for remaining in staging.rglob("*"):
                if remaining.is_file() and remaining.suffix.lower() in AUDIO_EXTS:
                    dest = QUARANTINE / remaining.relative_to(staging)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(remaining), dest)
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
               stamps: dict[str, str] | None = None) -> tuple[list[Path], list[Slot]] | None:
    """The ``.m3u`` paths playlist *name* gets from its store (homelab#1914),
    and the slots behind them: its entries resolved by Spotify ID or ISRC only,
    against the whole library, each file once and in store order; an ``album``
    playlist through :func:`playlists.album_paths` on the same entries.  Local
    entries are not in the view (:func:`_local_tail`).  None when its store
    can't be read: *name* comes from :func:`store.names`, so the file exists."""
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
        store.save(playlist)
    logger.info("  [STORE-TAIL] %s: %d local entr%s, %d resolved", name, len(playlist.locals),
                "y" if len(playlist.locals) == 1 else "ies", len(dropped))
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
            "held": held_urls(QUARANTINE, SPOTDL_DIR) | album_held(SPOTDL_DIR / albums.STATE_FILE.name),
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
    count per playlist, and :data:`EMPTY_SLOTS_NAME` the first
    :data:`EMPTY_SLOTS_LISTED` empty entries of each.

    An ``album`` playlist lists every local track of every album on it instead
    (#268, :func:`playlists.album_paths`).  A playlist whose store fails is
    logged and keeps its last ``.m3u``: a store failure never fails a scan or
    empties a playlist.

    The Later queue's available albums (homelab#2478, :func:`later_view`)
    follow the ``later`` store's albums in ``later.m3u`` while that album
    playlist has a store, and make up ``later.m3u`` alone once it has none.
    When the queue can't be read, ``later.m3u`` keeps its last content.
    """
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    names = store.names()
    if not names and not state.in_db():
        logger.debug("No playlist stores found — no playlists to generate")
        return {}

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
        queue_failed = False
        try:
            queue = later_view(library, by_album, by_record)
        except Exception:
            queue_failed = True
            logger.error("  [LATER] the Later queue can't be read — %s.m3u is left as it was", later_queue.PLAYLIST,
                         exc_info=True)
        for name in names:
            if name == later_queue.PLAYLIST and queue_failed:
                continue
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
            if name == later_queue.PLAYLIST and queue:
                listed = set(paths)
                paths = paths + [p for p in queue if p not in listed]
            counts[name] = _write_m3u(name, paths)
            empty[name] = playlists.empty_slots(slots)
            missing[name] = [f"{s.entry.artist} — {s.entry.name}" for s in slots if s.item is None]
        if later_queue.PLAYLIST not in names and queue is not None \
                and (queue or (PLAYLISTS / f"{later_queue.PLAYLIST}.m3u").exists()):
            counts[later_queue.PLAYLIST] = _write_m3u(later_queue.PLAYLIST, queue)

    if metrics is not None:
        metrics.slots_empty = empty
        _save_empty_slots(empty, missing)
    return counts


def _write_m3u(name: str, paths: list[Path]) -> int:
    lines = [os.path.relpath(p, PLAYLISTS) for p in paths]
    (PLAYLISTS / f"{name}.m3u").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def _later_albums() -> list[tuple[str, dict]] | None:
    """The Later queue's available albums, ``(key, record)`` in ``.m3u`` order,
    once the entries named by ref whose rows now have a release group have it
    (:func:`later_queue.resolve_refs`) and the pending albums the library now
    holds whole have become available (:func:`later_queue.mark_available`).
    None while the JSON files are the state; raises when the database can't
    be read."""
    with state.session() as conn:
        if conn is None:
            return None
        rows = state.load_albums(conn)
        later_queue.resolve_refs(conn, rows)
        later_queue.mark_available(conn, rows)
        return later_queue.albums(conn, rows)


def later_view(library: ItemIndex, by_album: dict[str, list], by_record: dict[str, list]) -> list[Path] | None:
    """The Later queue's ``.m3u`` paths (homelab#2478, :mod:`music_fetch.later_queue`):
    each available album's local tracks (:func:`album_tracks`), album by album
    in ``available_at`` order, each file once.  An import's album becomes
    available here (:func:`_later_albums`), so it joins the end at the scan
    that follows it.  None while the JSON files are the state; raises when
    the database can't be read."""
    found = _later_albums()
    if found is None:
        return None
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
    path = SPOTDL_DIR / EMPTY_SLOTS_NAME
    try:
        state.save_doc(state.EMPTY_SLOTS, path, report)
    except (OSError, state.Unavailable) as exc:
        logger.warning("Could not save the empty slots to %s: %s", path, exc)


def add_source(lib: MusicLibrary, have_source: str, new_source: str, tracks: list[list]) -> int:
    """Append *new_source*, and the track's Spotify ID, to the *have_source* items
    matching *tracks*.  Returns the count of items changed."""
    index = ItemIndex(lib.items_by_source(have_source))
    count = 0
    for track in map(PlaylistTrack.from_entry, tracks):
        item, _ = index.match_track(track)
        if item is not None and _link(item, new_source, track.song_id):
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
        return (not same_base_title(track.name, i.title or ""), (i.disc, i.track) != (track.disc, track.track))

    have.sort(key=key)
    if len(have) > 1 and key(have[0]) == key(have[1]):
        return None, True
    return (have[0] if have else None), False


def tag_album_ids(lib: MusicLibrary, source: str, tracks: list[list], since: float, tracks_count: int) -> int:
    """Record each album-mode playlist track's Spotify ID on the *source* item it maps to.

    Usenet files carry no Spotify IDs (#176).  Per track, first hit wins:
    1. ISRC among the item's ISRCs: this import's usenet items first, the one
       whose title (edition notes dropped) and then disc/track agree when
       several carry it, none (``[AMBIG]``) when that leaves a tie (#278);
       else any of *source*'s items;
    2. disc and track number, only on usenet items imported since *since*
       whose release has Spotify's track count (editions renumber tracks),
       and only when the title agrees too, edition notes dropped (#278): a
       number alone is not identity.  A file beets matched to another
       recording (*Love* tagged *Bodies*) gets no ID, and logs ``[NUMBER]``.
    Title words never give an ID (#279): the IDs are a best effort, and an
    album record's membership never depends on them (``album_record``,
    :mod:`music_scan.release`).  The words rung of #240 is gone.
    Local-only extras (``album_extra``) are left out (#294): an older copy
    that already holds a track's ID or ISRC would take the match, and the
    release would never get the ID that retires the extra
    (``release.retire_held``).
    Returns the count of items tagged.
    """
    from music_scan.release import EXTRA  # noqa: PLC0415

    index = ItemIndex(i for i in lib.items_by_source(source) if not i.get(EXTRA))
    imported = [i for i in index.items if i.get("via") == "usenet" and (i.added or 0) >= since]
    fresh = [i for i in imported if tracks_count and i.tracktotal == tracks_count]
    count = 0
    for track in map(PlaylistTrack.from_entry, tracks):
        if not track.song_id or track.song_id in index.by_id:
            continue
        match, ambiguous = _release_isrc_match(track, imported) if track.isrc else (None, False)
        if ambiguous:
            logger.warning("  [AMBIG] %s: %s — %s: several of this release's items carry ISRC %s and none is "
                           "told apart by title or disc/track; no ID", source, track.name, track.artist, track.isrc)
            continue
        if match is None:
            match, _ = index.match(None, track.isrc, words=False)
        if match is None and track.disc and track.track:
            at = next((i for i in fresh if (i.disc, i.track) == (track.disc, track.track)), None)
            if at is not None and same_base_title(track.name, at.title or ""):
                match = at
            elif at is not None:
                logger.info("  [NUMBER] %s: %s — %s is at %d-%d, but the item there is %r; no ID by number",
                            source, track.name, track.artist, track.disc, track.track, at.title)
        if match is None:
            logger.debug("  [NOID] %s: %s — %s matches no library item", source, track.name, track.artist)
            continue
        add_to_list(match, "spotify_ids", track.song_id)
        match.store()
        count += 1
    return count



def run() -> None:
    """Execute the full scan pipeline, push metrics on completion."""
    metrics = ScanMetrics()
    start = time.monotonic()

    try:
        logger.info("==> music-scan starting")

        quarantined_before = _count_quarantine()

        logger.info("==> Checking download lengths...")
        try:
            metrics.rejected = guard_inbox(SPOTDL_DIR, REJECTED, AUDIO_EXTS)
        except Exception:
            logger.error("Length guard failed — continuing with import", exc_info=True)

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

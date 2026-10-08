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
from pathlib import Path

from music_fetch.ingest import PendingRemovals
from music_scan import playlists
from music_scan.guard import guard_inbox
from music_scan.identity import ItemIndex, PlaylistTrack, add_to_list, item_spotify_ids, release_match, spotify_id
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


def run_inbox_import(skip_limit: int | None = None) -> list[tuple[str, str]]:
    """Snapshot inbox, run beet import, check names. Returns (title, artist) pairs."""
    if skip_limit is None:
        skip_limit_env = os.environ.get("BEET_SKIP_LIMIT")
        skip_limit = int(skip_limit_env) if skip_limit_env else None
    if skip_limit is not None:
        logger.info("Skip limit    : %d (early termination enabled)", skip_limit)
    inbox_snapshot = _snapshot_inbox(INBOX)
    logger.info("Inbox snapshot : %d audio file(s) queued for import", len(inbox_snapshot))
    import_start = time.time()
    run_beet_import(INBOX, skip_limit=skip_limit)
    with MusicLibrary(LIBRARY_DB) as lib:
        imported = lib.items_added_since(import_start)
    logger.info("Newly imported : %d track(s)", len(imported))
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


_spotdl_songs = playlists.spotdl_songs


def _item_path(item) -> Path:
    raw = item.path.decode() if isinstance(item.path, bytes) else item.path
    return Path(raw) if Path(raw).is_absolute() else LIBRARY / raw


def playlist_paths(slots: list[Slot], source_items: list, slot_fills: bool = True) -> list[Path]:
    """The ``.m3u`` lines of a playlist: its resolved slots in playlist order
    (each file once), then the *source_items* no entry matched, alphabetically.
    Without *slot_fills* the slots filled from the whole library are left out:
    today's path, which the result always contains (#228)."""
    ordered: list[Path] = []
    matched: set[Path] = set()
    for slot in slots:
        if slot.item is None or (not slot_fills and slot.filled_from_library):
            continue
        p = _item_path(slot.item)
        if p not in matched:
            ordered.append(p)
            matched.add(p)
    unmatched = sorted(p for p in (_item_path(item) for item in source_items) if p not in matched)
    return ordered + unmatched


def regen_playlists(metrics: ScanMetrics | None = None) -> dict[str, int]:
    """Regenerate .m3u files for every .spotdl playlist. Returns {name: track_count}.

    Each entry is a slot (:mod:`music_scan.playlists`): resolved to an item tagged
    with the playlist by the identity ladder as before, else to any library item
    by Spotify ID or ISRC (``[SLOT]``), so a track imported by any route reaches
    every playlist that lists it at the next scan, with no download and no tag
    write (#228).  *metrics*, when given, takes the empty-slot count per playlist.
    """
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    spotdl_files = sorted(SPOTDL_DIR.glob("*.spotdl"))
    if not spotdl_files:
        logger.debug("No .spotdl files found — no playlists to generate")
        return {}

    counts: dict[str, int] = {}
    empty: dict[str, int] = {}
    with MusicLibrary(LIBRARY_DB) as lib:
        library = ItemIndex(lib.all_items())
        for spotdl_file in spotdl_files:
            name = spotdl_file.stem
            m3u = PLAYLISTS / f"{name}.m3u"

            source_items = lib.items_by_source(name)
            slots = playlists.resolve(playlists.playlist_entries(spotdl_file), ItemIndex(source_items), library)
            playlists.log_slots(name, slots)

            lines = [os.path.relpath(p, PLAYLISTS) for p in playlist_paths(slots, source_items)]
            m3u.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            counts[name] = len(lines)
            empty[name] = playlists.empty_slots(slots)

    if metrics is not None:
        metrics.slots_empty = empty
    return counts


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
    """Tag the library item holding a .spotdl *song* with *source* instead of downloading it (#187).

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


def tag_album_ids(lib: MusicLibrary, source: str, tracks: list[list], since: float, tracks_count: int) -> int:
    """Record each album-mode playlist track's Spotify ID on the *source* item it maps to.

    Usenet files carry no Spotify IDs (#176).  Per track, first hit wins:
    1. ISRC among the item's ISRCs;
    2. disc and track number, only on usenet items imported since *since*
       whose release has Spotify's track count (editions renumber tracks);
    3. title+artist words, logged so the fallback rate is visible;
    4. the same title among this import's usenet items, with the artist
       anywhere in a credit that may add ``feat.`` names (#240), logged too.
    Returns the count of items tagged.
    """
    index = ItemIndex(lib.items_by_source(source))
    album_ids = {t.song_id for t in map(PlaylistTrack.from_entry, tracks) if t.song_id}
    imported = [i for i in index.items if i.get("via") == "usenet" and (i.added or 0) >= since]
    fresh = [i for i in imported if tracks_count and i.tracktotal == tracks_count]
    # Rung 4 takes each item once, and never one already holding a track of this album.
    unclaimed = [i for i in imported if not item_spotify_ids(i) & album_ids]
    count = 0
    for track in map(PlaylistTrack.from_entry, tracks):
        if not track.song_id or track.song_id in index.by_id:
            continue
        match, _ = index.match(None, track.isrc, words=False)
        if match is None and track.disc and track.track:
            match = next((i for i in fresh if (i.disc, i.track) == (track.disc, track.track)), None)
        if match is None:
            match, _ = index.match(None, None, track.name, track.artist)
            if match is None:
                match = release_match(track.name, track.artist, unclaimed)
            if match is not None:
                logger.info("  [WORDS] %s: %s — %s matched by title+artist only", source, track.name, track.artist)
        if match in unclaimed:
            unclaimed.remove(match)
        if match is None:
            logger.info("  [NOID] %s: %s — %s matches no library item", source, track.name, track.artist)
            continue
        add_to_list(match, "spotify_ids", track.song_id)
        match.store()
        count += 1
    return count


def apply_pending_removals(pending: PendingRemovals, lib: MusicLibrary) -> int:
    """Clear beets source tags for tracks and playlists in *pending*.

    Logs one ``[UNLINK]`` line per item cleared, whichever rung matched it.
    Returns the number of items modified; entries with no library item are
    logged and counted separately (#190).
    """
    logger.info(
        "==> Processing pending removals: %d track(s), %d source(s)...",
        len(pending.tracks),
        len(pending.remove_sources),
    )
    total = not_found = 0
    for track in pending.tracks:
        items = lib.clear_source_tag(
            title=track.title, artist=track.artist, source=track.source,
            spotify_id=track.spotify_id, isrc=track.isrc,
        )
        if not items:
            logger.warning(
                "  WARNING: not found in beets — may need manual cleanup: %s by %s (source=%s)",
                track.title,
                track.artist,
                track.source,
            )
            not_found += 1
        for item in items:
            logger.info("  [UNLINK] %s: %s — %s", track.source, item.get("title"), item.get("artist"))
        total += len(items)

    for source_name in pending.remove_sources:
        logger.info("==> Removing all tracks from playlist: %s", source_name)
        items = lib.items_by_source(source_name)
        for item in items:
            parts = [p for p in (item.get("sources") or "").split(",") if p.strip() and p.strip() != source_name]
            item["sources"] = ",".join(parts)
            item.store()
        m3u = PLAYLISTS / f"{source_name}.m3u"
        m3u.unlink(missing_ok=True)
        logger.info("  Cleared %d item(s) and removed .m3u for source=%s", len(items), source_name)
        total += len(items)

    logger.info("Cleared the source tag on %d item(s); %d removed entr(ies) not found", total, not_found)
    return total


def run(pending: PendingRemovals | None = None) -> None:
    """Execute the full scan pipeline, push metrics on completion."""
    metrics = ScanMetrics()
    start = time.monotonic()

    try:
        logger.info("==> music-scan starting")

        metrics.tracks_removed = 0
        if pending is not None:
            logger.info("==> Applying pending removals...")
            try:
                with MusicLibrary(LIBRARY_DB) as lib:
                    metrics.tracks_removed = apply_pending_removals(pending, lib)
            except Exception:
                logger.error("Pending-removals step failed — continuing with import", exc_info=True)

        quarantined_before = _count_quarantine()

        logger.info("==> Checking download lengths...")
        try:
            metrics.rejected = guard_inbox(SPOTDL_DIR, SPOTDL_DIR, REJECTED, AUDIO_EXTS)
        except Exception:
            logger.error("Length guard failed — continuing with import", exc_info=True)

        logger.info("==> Importing from inbox...")
        since = time.time()
        imported = run_inbox_import()

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
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.lossless_items = count_lossless_items()
        metrics.push()

"""Playlist slots (#228): one slot per playlist entry, filled by resolving the
entry against the library at every scan.

A playlist is its ordered list of entries.  Whether an entry is "downloaded" is
not recorded anywhere: it is the entry resolving to a library item, recomputed
every time (the first slice of homelab#1914, whose store will replace the
``.spotdl`` reader in :func:`playlist_entries`).

Resolution is additive over today's path.  An entry resolves

1. as before, to an item tagged with the playlist in ``sources``, by the full
   identity ladder (Spotify ID, ISRC, then title+artist words, ``[WORDS]``);
2. otherwise to any library item by Spotify ID, then ISRC (``slot:id``,
   ``slot:isrc``): a track that arrived by another route (a Usenet album,
   another playlist's download, a manual import) is shown without a download
   and without a ``sources`` write.

Nothing a playlist shows today can disappear: phase 2 only fills the slots
phase 1 leaves empty.  ``music-audit-playlists`` (:func:`main`) prints the
proof per playlist, read-only, and accounts for every entry and every line:
shared files (#232), tagged items no entry matches, and empty entries whose
track sits in ``quarantine/replaced/`` (#234).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import NamedTuple

from music_scan.identity import (
    BY_ID, BY_ISRC, BY_WORDS, ItemIndex, PlaylistTrack, name_words, spotify_id,
)

logger = logging.getLogger(__name__)

# Rungs of the whole-library phase, distinct from the source rungs.
BY_SLOT_ID, BY_SLOT_ISRC = "slot:id", "slot:isrc"
SLOT_RUNGS = frozenset({BY_SLOT_ID, BY_SLOT_ISRC})
_SLOT_RUNG = {BY_ID: BY_SLOT_ID, BY_ISRC: BY_SLOT_ISRC}


class Entry(NamedTuple):
    """One playlist entry, whichever record it came from."""

    name: str
    artist: str
    song_id: str | None = None
    isrc: str | None = None

    @classmethod
    def from_song(cls, song: dict) -> "Entry":
        """From a ``.spotdl`` song entry."""
        return cls(
            song.get("name", ""), (song.get("artists") or [""])[0],
            song.get("song_id") or spotify_id(song.get("url")), song.get("isrc") or None,
        )

    @classmethod
    def from_track(cls, track: PlaylistTrack) -> "Entry":
        """From an album record's per-playlist track (``.albums.json``)."""
        return cls(track.name, track.artist, track.song_id or None, track.isrc or None)

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.name}" if self.artist else self.name


class Slot(NamedTuple):
    entry: Entry
    item: object | None
    rung: str | None

    @property
    def filled_from_library(self) -> bool:
        return self.rung in SLOT_RUNGS


def spotdl_songs(spotdl_file: Path) -> list[dict]:
    """The song entries of a ``.spotdl`` file, in playlist order; ``[]`` when the
    file is absent, unreadable or has no songs."""
    try:
        return json.loads(spotdl_file.read_text(encoding="utf-8")).get("songs", [])
    except Exception:
        return []


def playlist_entries(spotdl_file: Path) -> list[Entry]:
    """The playlist's entries in order.  Read from its ``.spotdl`` today: for an
    ``album`` playlist that is the whole Spotify list, for a spotdl one the
    downloaded and linked entries only (deferred ones come back as new).
    homelab#1914's store replaces this reader."""
    return [Entry.from_song(song) for song in spotdl_songs(spotdl_file)]


def resolve(entries: list[Entry], source: ItemIndex, library: ItemIndex | None = None) -> list[Slot]:
    """One slot per entry, in order.  Today's path first (*source*: the items
    tagged with the playlist, full ladder), then *library* by Spotify ID and
    ISRC for the entries still empty.  Computed, never recorded."""
    slots: list[Slot] = []
    for entry in entries:
        item, rung = source.match(entry.song_id, entry.isrc, entry.name, entry.artist)
        if item is None and library is not None:
            item, rung = library.match(entry.song_id, entry.isrc, words=False)
            rung = _SLOT_RUNG.get(rung) if rung else None
        slots.append(Slot(entry, item, rung))
    return slots


def empty_slots(slots: list[Slot]) -> int:
    return sum(1 for s in slots if s.item is None)


def log_slots(name: str, slots: list[Slot]) -> None:
    """One summary line per playlist for the slot fills, per track at debug,
    and the ``[WORDS]`` rate as before."""
    filled = [s for s in slots if s.filled_from_library]
    for slot in filled:
        logger.debug("  [SLOT] %s: %s filled from the library (%s)", name, slot.entry.label, slot.rung)
    if filled:
        logger.info("  [SLOT] %s: %d entr(ies) filled from the library by ID/ISRC", name, len(filled))
    matched = [s for s in slots if s.item is not None]
    words = [s for s in matched if s.rung == BY_WORDS]
    for slot in words:
        logger.debug("  [WORDS] %s: %s matched by title+artist only", name, slot.entry.name)
    if words:
        logger.info("  %s: %d of %d track(s) matched by title+artist only", name, len(words), len(matched))


# ---------------------------------------------------------------------------
# music-audit-playlists: the read-only proof that nothing disappears
# ---------------------------------------------------------------------------


def _flex(item, field: str) -> str:
    return item.get(field) or "-"


def _describe(item) -> str:
    """One line of an item's identity for the audit: id, title/artist, IDs."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    return (f"item {getattr(item, 'id', None) or '?'} {_item_path(item).name}: {item.artist or item.albumartist or '?'} — "
            f"{item.title or '?'}  spotify_ids={_flex(item, 'spotify_ids')} isrc={_flex(item, 'isrc')}")


def tail_items(slots: list[Slot], source_items: list) -> list:
    """Items tagged with the playlist that no entry resolves to (#234): today's
    alphabetical tail of the ``.m3u``.  A tag the #202 repair restored, or a
    track Spotify no longer lists, shows up here."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    matched = {_item_path(s.item) for s in slots if s.item is not None}
    return [item for item in source_items if _item_path(item) not in matched]


class ReplacedFile(NamedTuple):
    """A file under ``quarantine/replaced/`` (a #210 merge's loser, named
    ``<loser item id>-<file name>``, or ``music-audit-lengths --replace``'s old
    audio) and the identity its tags still carry."""

    path: Path
    title: str
    artist: str
    spotify_ids: frozenset[str]
    isrcs: frozenset[str]
    mb_trackid: str | None

    @property
    def words(self) -> frozenset[str]:
        return name_words(f"{self.title} {self.artist}")


def _file_tags(path: Path) -> ReplacedFile | None:
    """Read a replaced file's identity from its tags; None when unreadable.
    beets writes ISRC and the MusicBrainz track id to the file, so those survive
    the merge; spotdl's Spotify atoms are usually scrubbed at import."""
    try:
        from mediafile import MediaFile  # noqa: PLC0415

        from music_scan.music_pipeline import _read_spotdl_tags  # noqa: PLC0415

        mf = MediaFile(str(path))
        spotdl = _read_spotdl_tags(str(path))
        ids = {sid for sid in (spotify_id(spotdl.url),) if sid}
        isrcs = {c.strip() for c in (mf.isrc or "").split(";") if c.strip()}
        if spotdl.isrc:
            isrcs.add(spotdl.isrc)
        return ReplacedFile(path, mf.title or "", mf.artist or mf.albumartist or "", frozenset(ids), frozenset(isrcs),
                            mf.mb_trackid or None)
    except Exception:
        logger.debug("Could not read tags of %s", path, exc_info=True)
        return None


def replaced_files(replaced_dir: Path) -> list[ReplacedFile]:
    from music_scan.scan import AUDIO_EXTS  # noqa: PLC0415

    if not replaced_dir.is_dir():
        return []
    files = sorted(f for f in replaced_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    return [rf for rf in map(_file_tags, files) if rf is not None]


def replaced_matches(slots: list[Slot], replaced: list[ReplacedFile]) -> list[tuple[int, Slot, ReplacedFile]]:
    """Empty slots whose Spotify ID or ISRC a replaced file carries (#234): the
    entry's track left the library with a merge or a replace."""
    by_id = {sid: rf for rf in replaced for sid in rf.spotify_ids}
    by_isrc = {code: rf for rf in replaced for code in rf.isrcs}
    found = []
    for pos, slot in enumerate(slots, 1):
        if slot.item is not None:
            continue
        rf = by_id.get(slot.entry.song_id or "") or by_isrc.get(slot.entry.isrc or "")
        if rf is not None:
            found.append((pos, slot, rf))
    return found


def keeper_candidates(rf: ReplacedFile, library: ItemIndex) -> list[tuple[str, object]]:
    """Library items a merge could have kept for *rf*: same ISRC, same
    MusicBrainz track id, or the same title+artist words."""
    seen: set[int] = set()
    out: list[tuple[str, object]] = []

    def add(why: str, item) -> None:
        if item is not None and id(item) not in seen:
            seen.add(id(item))
            out.append((why, item))

    for code in sorted(rf.isrcs):
        add(f"isrc {code}", library.by_isrc.get(code))
    if rf.mb_trackid:
        for item in library.items:
            if item.get("mb_trackid") == rf.mb_trackid:
                add(f"mb_trackid {rf.mb_trackid}", item)
    if rf.words:
        add("title+artist words", library.by_words.get(rf.words))
    return out



def collisions(slots: list[Slot]) -> dict[Path, list[tuple[int, Slot]]]:
    """Entries resolving to one file (#232): ``{path: [(position, slot), ...]}``
    for every file two or more slots share.  Regen writes such a file once, so
    the ``.m3u`` has fewer lines than the playlist has entries; the first case
    was #210's duplicate merge (wedding 171 entries, 170 lines)."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    groups: dict[Path, list[tuple[int, Slot]]] = {}
    for pos, slot in enumerate(slots, 1):
        if slot.item is not None:
            groups.setdefault(_item_path(slot.item), []).append((pos, slot))
    return {path: group for path, group in groups.items() if len(group) > 1}


def audit(replaced_dir: Path | None = None) -> int:
    """Per playlist, compare the ``.m3u`` on disk, today's path and the slots.
    Prints the entries the slots add, any the file on disk would lose, the
    entries that share one file (shown once in the ``.m3u``), the tagged items
    no entry resolves to (the tail lines), and the empty entries whose track is
    in *replaced_dir* (``quarantine/replaced/``) with the items a merge could
    have kept, so every entry and every line is accounted for.  Returns the
    number of lost entries (0 is the expected answer); the rest are warnings."""
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.scan import PLAYLISTS, QUARANTINE, SPOTDL_DIR, _item_path, playlist_paths  # noqa: PLC0415

    replaced = replaced_files(QUARANTINE / "replaced" if replaced_dir is None else replaced_dir)
    lost_total = shared_total = groups_total = tail_total = replaced_total = 0
    with MusicLibrary(LIBRARY_DB) as lib:
        library = ItemIndex(lib.all_items())
        for spotdl_file in sorted(SPOTDL_DIR.glob("*.spotdl")):
            name = spotdl_file.stem
            source_items = lib.items_by_source(name)
            slots = resolve(playlist_entries(spotdl_file), ItemIndex(source_items), library)
            today = [os.path.relpath(p, PLAYLISTS) for p in playlist_paths(slots, source_items, slot_fills=False)]
            new = [os.path.relpath(p, PLAYLISTS) for p in playlist_paths(slots, source_items)]
            m3u = PLAYLISTS / f"{name}.m3u"
            on_disk = m3u.read_text(encoding="utf-8").splitlines() if m3u.exists() else []
            filled = {os.path.relpath(_item_path(s.item), PLAYLISTS): s for s in slots if s.filled_from_library}
            added = [line for line in new if line not in set(on_disk)]
            lost = [line for line in on_disk if line not in set(new)]
            lost_total += len(lost)
            shared = collisions(slots)
            shared_entries = sum(len(g) for g in shared.values())
            shared_total += shared_entries
            groups_total += len(shared)
            resolved = len(slots) - empty_slots(slots)
            tail = tail_items(slots, source_items)
            tail_total += len(tail)
            gone = replaced_matches(slots, replaced)
            replaced_total += len(gone)
            logger.info(
                "%s: on disk %d | today's path %d | with slots %d | filled from library %d | "
                "entries %d = own file %d + shared file %d + empty %d | tail lines %d | empty in replaced %d",
                name, len(on_disk), len(today), len(new), len(filled),
                len(slots), resolved - shared_entries, shared_entries, empty_slots(slots), len(tail), len(gone),
            )
            for line in added:
                slot = filled.get(line)
                why = f"{slot.rung}: {slot.entry.label}" if slot else "not in the file on disk"
                logger.info("  + %s  (%s)", line, why)
            for line in lost:
                logger.warning("  - %s  (on disk, resolves no more)", line)
            for path, group in shared.items():
                item = group[0][1].item
                logger.warning("  = %s (item %s) is the file of %d entries, written once:",
                               os.path.relpath(path, PLAYLISTS), getattr(item, "id", None) or "?", len(group))
                for pos, slot in group:
                    e = slot.entry
                    logger.warning("      #%d %s  id=%s isrc=%s (%s)", pos, e.label, e.song_id or "-", e.isrc or "-", slot.rung)
            for item in tail:
                logger.warning("  ~ %s  (tagged sources=%s, no entry matches it by ID, ISRC or words)",
                               _describe(item), _flex(item, "sources"))
            for pos, slot, rf in gone:
                e = slot.entry
                logger.warning("  ? #%d %s  id=%s isrc=%s: empty, but %s carries it (isrc=%s mb_trackid=%s)",
                               pos, e.label, e.song_id or "-", e.isrc or "-", rf.path.name,
                               ";".join(sorted(rf.isrcs)) or "-", rf.mb_trackid or "-")
                candidates = keeper_candidates(rf, library)
                for why, item in candidates:
                    logger.warning("        keeper? by %s: %s", why, _describe(item))
                if not candidates:
                    logger.warning("        no library item shares its ISRC, MusicBrainz track id or words")
    if tail_total:
        logger.warning("%d .m3u line(s) come from tagged items no entry resolves to", tail_total)
    if replaced_total:
        logger.warning("%d empty entr(ies) have their track in quarantine/replaced/ (merged or replaced away)", replaced_total)
    if lost_total:
        logger.warning("%d entr(ies) would disappear", lost_total)
    elif shared_total:
        logger.warning(
            "No playlist loses an entry, but %d entr(ies) in %d group(s) share a file with another entry "
            "and are in the .m3u once (duplicate merges, music-pipeline#210); whether a file may appear twice is open",
            shared_total, groups_total,
        )
    else:
        logger.info("No playlist loses an entry; every entry has its own file or is empty")
    return lost_total


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-playlists", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    raise SystemExit(1 if audit() else 0)

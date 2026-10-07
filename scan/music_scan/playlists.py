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
proof per playlist, read-only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import NamedTuple

from music_scan.identity import BY_ID, BY_ISRC, BY_WORDS, ItemIndex, PlaylistTrack, spotify_id

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


def audit() -> int:
    """Per playlist, compare the ``.m3u`` on disk, today's path and the slots.
    Prints the entries the slots add and any the file on disk would lose.
    Returns the number of lost entries (0 is the expected answer)."""
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.scan import PLAYLISTS, SPOTDL_DIR, _item_path, playlist_paths  # noqa: PLC0415

    lost_total = 0
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
            logger.info(
                "%s: on disk %d | today's path %d | with slots %d | filled from library %d | empty slots %d",
                name, len(on_disk), len(today), len(new), len(filled), empty_slots(slots),
            )
            for line in added:
                slot = filled.get(line)
                why = f"{slot.rung}: {slot.entry.label}" if slot else "not in the file on disk"
                logger.info("  + %s  (%s)", line, why)
            for line in lost:
                logger.warning("  - %s  (on disk, resolves no more)", line)
    if lost_total:
        logger.warning("%d entr(ies) would disappear", lost_total)
    else:
        logger.info("No playlist loses an entry")
    return lost_total


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-playlists", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    raise SystemExit(1 if audit() else 0)

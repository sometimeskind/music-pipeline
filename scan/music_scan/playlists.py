"""Playlist slots (#228): one slot per playlist entry, filled by resolving the
entry against the library at every scan.

A playlist is its ordered list of entries, its store's (homelab#1914).
Whether an entry is "downloaded" is not recorded anywhere: it is the entry
resolving to a library item, recomputed every time.  The store view resolves
each entry to any library item by Spotify ID, then ISRC (``slot:id``,
``slot:isrc``): a track that arrived by any route (a Usenet album, another
playlist's download, a manual import) is shown without a download and without
a ``sources`` write.  :func:`resolve` can also take a first, narrower index
(the album hooks' this-import items, full identity ladder).

An ``album`` playlist is listed at the album level (#268,
:func:`album_paths`): every local track of every album on it, album by album
in playlist order, each album in disc/track order.  The entries only decide
which albums are on it.
"""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
from typing import NamedTuple

from music_scan.identity import (
    BY_ID, BY_ISRC, BY_WORDS, ItemIndex, PlaylistTrack, name_words, recording_ids,
    spotify_id,
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
        """From a playlist store song entry."""
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


def resolve(entries: list[Entry], source: ItemIndex, library: ItemIndex | None = None) -> list[Slot]:
    """One slot per entry, in order.  *source* first (full ladder; empty for
    the store view), then *library* by Spotify ID and ISRC for the entries
    still empty.  Computed, never recorded."""
    slots: list[Slot] = []
    for entry in entries:
        item, rung = source.match(entry.song_id, entry.isrc, entry.name, entry.artist)
        if item is None and library is not None:
            item, rung = library.match(entry.song_id, entry.isrc, words=False)
            rung = _SLOT_RUNG.get(rung) if rung else None
        slots.append(Slot(entry, item, rung))
    return slots


# ---------------------------------------------------------------------------
# Album playlists (#268): every local track of every album on the playlist
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s or "").casefold().split())


def by_album_id(items) -> dict[str, list]:
    """The *items* per ``spotify_album_id`` (canon's album, #209)."""
    out: dict[str, list] = {}
    for item in items:
        if item.get("spotify_album_id"):
            out.setdefault(item.get("spotify_album_id"), []).append(item)
    return out


def record_stamps(records: dict[str, dict]) -> dict[str, str]:
    """Each album record's primary Spotify album ID → its key (homelab#2478)."""
    from music_fetch.state import primary_spotify  # noqa: PLC0415

    return {sid: key for key, record in records.items() if (sid := primary_spotify(record))}


def album_groups(songs: list[dict]) -> dict[str, list[dict]]:
    """The playlist's *songs* per album (``albums.album_key``), albums in
    playlist order."""
    from music_fetch.albums import album_key  # noqa: PLC0415

    groups: dict[str, list[dict]] = {}
    for song in songs:
        groups.setdefault(album_key(song), []).append(song)
    return groups


def album_items(album_id: str | None, name: str, slots: list[Slot], by_album: dict[str, list],
                stamp: str | None = None) -> list:
    """The local tracks of the Spotify album *album_id* named *name*, whose
    playlist entries resolved to *slots*: the items they resolve to, and
    every item canon filed on the album.  That is *album_id*, plus the album a
    resolved item sits on under the same name (a #214 twin's representative).
    A resolved item canon moved to another album (a single's track filed on
    its standard album) is listed alone, not with that album.  The release's
    local-only extras are on its album by #245.  In disc/track order.

    An album record's release is the album (#279): once its items carry
    ``album_record`` *stamp* (the key of the record whose primary Spotify album
    *album_id* is; *album_id* itself when not given, as before homelab#2478),
    they alone are listed, whatever the slots resolve to (a same recording
    kept on another album stays there)."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    def ordered(items) -> list:
        return sorted(items, key=lambda i: (i.disc or 1, i.track or 0, str(_item_path(i))))

    stamp = stamp or album_id
    release = [i for i in by_album.get(album_id, []) if i.get("album_record") == stamp] if album_id else []
    if release:
        return ordered(release)
    found = [s.item for s in slots if s.item is not None]
    ids = ({album_id} if album_id else set()) | {i.get("spotify_album_id") for i in found
                   if i.get("spotify_album_id") and _norm(i.album) == _norm(name)}
    items: dict = {}
    for item in [*found, *(i for album_id in sorted(ids) for i in by_album.get(album_id, []))]:
        items.setdefault(_item_path(item), item)
    return ordered(items.values())


def album_paths(songs: list[dict], source: ItemIndex, library: ItemIndex,
                by_album: dict[str, list], stamps: dict[str, str] | None = None
                ) -> tuple[list[Path], list[Slot], int]:
    """An album playlist's ``.m3u`` lines (#268): ``(paths, slots, albums)``.
    Every album at least one of *songs* is on, in playlist order, lists its
    :func:`album_items`, each file once.  An album with no local track yet
    lists nothing.  *slots* are the entries' slots, for the empty count.
    *stamps*: each album record's primary Spotify album ID → its key, the
    ``album_record`` its release's items carry (:func:`record_stamps`)."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    paths: list[Path] = []
    seen: set[Path] = set()
    all_slots: list[Slot] = []
    groups = album_groups(songs)
    for key, group in groups.items():
        slots = resolve([Entry.from_song(s) for s in group], source, library)
        all_slots += slots
        for item in album_items(key, group[0].get("album_name", ""), slots, by_album, (stamps or {}).get(key)):
            p = _item_path(item)
            if p not in seen:
                seen.add(p)
                paths.append(p)
    return paths, all_slots, len(groups)


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

    @property
    def recording_ids(self) -> frozenset[str]:
        """Its recording's identity keys, for :class:`music_scan.identity.Recordings` (#287)."""
        return recording_ids(self.spotify_ids, self.isrcs, self.mb_trackid)


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

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
phase 1 leaves empty.

An ``album`` playlist is listed at the album level (#268,
:func:`album_paths`): every local track of every album on it, album by album
in playlist order, each album in disc/track order.  The entries only decide
which albums are on it.  ``music-audit-playlists`` (:func:`main`) prints the
proof per playlist, read-only, and accounts for every entry and every line:
shared files (#232), tagged items no entry matches, and empty entries whose
track sits in ``quarantine/replaced/`` (#234).  It skips album playlists;
``--albums`` reports those (:func:`album_audit`).  ``--clear-tail <playlist>``
clears that playlist's tag on its tail items that carry a Spotify ID, the
recovery for removals a crashed scan lost (#259); dry run unless ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import unicodedata
from pathlib import Path
from typing import NamedTuple

from music_scan.identity import (
    BY_ID, BY_ISRC, BY_WORDS, ItemIndex, PlaylistTrack, item_isrcs, item_spotify_ids, name_words, spotify_id,
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


def album_groups(songs: list[dict]) -> dict[str, list[dict]]:
    """The playlist's *songs* per album (``albums.album_key``), albums in
    playlist order."""
    from music_fetch.albums import album_key  # noqa: PLC0415

    groups: dict[str, list[dict]] = {}
    for song in songs:
        groups.setdefault(album_key(song), []).append(song)
    return groups


def album_items(key: str, name: str, slots: list[Slot], by_album: dict[str, list]) -> list:
    """The local tracks of the album *key* (Spotify album ID) named *name*,
    whose playlist entries resolved to *slots*: the items they resolve to, and
    every item canon filed on the album.  That is *key*, plus the album a
    resolved item sits on under the same name (a #214 twin's representative).
    A resolved item canon moved to another album (a single's track filed on
    its standard album) is listed alone, not with that album.  The release's
    local-only extras are on its album by #245.  In disc/track order."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    found = [s.item for s in slots if s.item is not None]
    ids = {key} | {i.get("spotify_album_id") for i in found
                   if i.get("spotify_album_id") and _norm(i.album) == _norm(name)}
    items: dict = {}
    for item in [*found, *(i for album_id in sorted(ids) for i in by_album.get(album_id, []))]:
        items.setdefault(_item_path(item), item)
    return sorted(items.values(), key=lambda i: (i.disc or 1, i.track or 0, str(_item_path(i))))


def album_paths(songs: list[dict], source: ItemIndex, library: ItemIndex,
                by_album: dict[str, list]) -> tuple[list[Path], list[Slot], int]:
    """An album playlist's ``.m3u`` lines (#268): ``(paths, slots, albums)``.
    Every album at least one of *songs* is on, in playlist order, lists its
    :func:`album_items`, each file once.  An album with no local track yet
    lists nothing.  *slots* are the entries' slots, for the empty count."""
    from music_scan.scan import _item_path  # noqa: PLC0415

    paths: list[Path] = []
    seen: set[Path] = set()
    all_slots: list[Slot] = []
    groups = album_groups(songs)
    for key, group in groups.items():
        slots = resolve([Entry.from_song(s) for s in group], source, library)
        all_slots += slots
        for item in album_items(key, group[0].get("album_name", ""), slots, by_album):
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

    try:
        if not replaced_dir.is_dir():
            return []
        files = sorted(f for f in replaced_dir.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    except OSError as exc:  # unreadable (CI runs the tests as a non-root user)
        logger.warning("Cannot read %s: %s", replaced_dir, exc)
        return []
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
            if (SPOTDL_DIR / f"{name}.album").exists():
                logger.info("%s: album playlist, listed at the album level — see --albums", name)
                continue
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


# ---------------------------------------------------------------------------
# --albums: what album-level listing changes (#268), read-only
# ---------------------------------------------------------------------------


def album_audit(state_file: Path | None = None) -> dict[str, tuple[int, int, int]]:
    """Per album playlist, the ``.m3u`` on disk, the track-level listing
    (before #268) and the album-level one (after), with the lines the
    album level adds and drops; then the album records: those whose old
    ``partial``/``gap`` fallback the next tick settles to ``imported``, and
    the ``have``/``filled`` albums whose library copy is not whole (left as
    they are).  Writes nothing.  Returns ``{playlist: (on disk, track level,
    album level)}``."""
    from music_fetch.albums import FILLED, HAVE, STATE_FILE, State, settle_imported  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.scan import PLAYLISTS, SPOTDL_DIR, have_album, playlist_paths  # noqa: PLC0415

    counts: dict[str, tuple[int, int, int]] = {}
    with MusicLibrary(LIBRARY_DB) as lib:
        all_items = lib.all_items()
        library = ItemIndex(all_items)
        by_album = by_album_id(all_items)
        sources: dict[str, ItemIndex] = {}
        for spotdl_file in sorted(SPOTDL_DIR.glob("*.spotdl")):
            name = spotdl_file.stem
            if not (SPOTDL_DIR / f"{name}.album").exists():
                continue
            source_items = lib.items_by_source(name)
            sources[name] = ItemIndex(source_items)
            songs = spotdl_songs(spotdl_file)
            track_level = [os.path.relpath(p, PLAYLISTS) for p in playlist_paths(
                resolve([Entry.from_song(s) for s in songs], sources[name], library), source_items)]
            paths, slots, n_albums = album_paths(songs, sources[name], library, by_album)
            album_level = [os.path.relpath(p, PLAYLISTS) for p in paths]
            m3u = PLAYLISTS / f"{name}.m3u"
            on_disk = m3u.read_text(encoding="utf-8").splitlines() if m3u.exists() else []
            counts[name] = (len(on_disk), len(track_level), len(album_level))
            empty_albums = sum(1 for key, group in album_groups(songs).items()
                               if not album_items(key, group[0].get("album_name", ""),
                                                  resolve([Entry.from_song(s) for s in group], sources[name], library),
                                                  by_album))
            logger.info("%s: on disk %d | track level %d | album level %d | %d entries on %d album(s), "
                        "%d album(s) with no local track yet", name, len(on_disk), len(track_level),
                        len(album_level), len(songs), n_albums, empty_albums)
            before = set(track_level)
            after = set(album_level)
            for line in album_level:
                if line not in before:
                    logger.info("  + %s", line)
            for line in track_level:
                if line not in after:
                    logger.info("  - %s  (on no album the playlist holds a track of)", line)

        state = State.load(state_file or SPOTDL_DIR / STATE_FILE.name)
        settle = not_whole = 0
        for key, record in state.albums.items():
            label = f"{key}: {record.get('artist')} — {record.get('name')}"
            if settle_imported(json.loads(json.dumps(record))):
                settle += 1
                n = sum(len(ids) for ids in record.get("fallback_tracks", {}).values())
                logger.info("  [SETTLE] %s: fallback (%s) → imported; %d track(s) leave the spotdl queue",
                            label, record.get("fallback_from"), n)
            elif record.get("status") in (HAVE, FILLED) and record.get("playlists"):
                def index(pl: str) -> ItemIndex:
                    if pl not in sources:
                        sources[pl] = ItemIndex(lib.items_by_source(pl))
                    return sources[pl]
                if not have_album(index, library, by_album, key, record):
                    not_whole += 1
                    logger.info("  [NOTWHOLE] %s (%s, %d tracks on Spotify): kept as it is", label,
                                record.get("status"), record.get("tracks_count") or 0)
        logger.info("Album records: %d fallback(s) the next tick settles to imported; "
                    "%d have/filled album(s) not whole in the library, left as they are", settle, not_whole)
    return counts


# ---------------------------------------------------------------------------
# --clear-tail: the recovery for removals a crashed scan dropped (#259)
# ---------------------------------------------------------------------------


def clear_tail(lib, name: str, spotdl_dir: Path, apply: bool = False) -> list:
    """Clear *name* from the ``sources`` of its tail items that carry a Spotify ID.

    A tail item is tagged with the playlist but no entry resolves to it (the
    audit's ``~`` lines).  One with a Spotify ID is an identified recording no
    entry agrees with by Spotify ID or ISRC, so by the identity rule it is not
    on the playlist: a removal that was never applied.  One without is left
    tagged: a successful release's local-only extra (#245, #238) has no entry
    on purpose, and an unidentified item can't be judged by ID.  Refuses an
    empty or unreadable ``.spotdl``, which would make every tagged item a tail
    item.  Returns the items cleared (or, in a dry run, that would be).
    """
    if (spotdl_dir / f"{name}.album").exists():
        # Its other tracks carry Spotify IDs no entry has, and belong on it (#268).
        raise SystemExit(f"{name}: an album playlist lists every track of its albums, tagged or not — nothing to clear")
    entries = playlist_entries(spotdl_dir / f"{name}.spotdl")
    if not entries:
        raise SystemExit(f"{name}: no entries in {spotdl_dir / f'{name}.spotdl'} — refusing to treat every item as tail")
    source_items = [i for i in lib.items_by_source(name)
                    if name in [p.strip() for p in (i.get("sources") or "").split(",")]]
    slots = resolve(entries, ItemIndex(source_items), ItemIndex(lib.all_items()))
    tail = tail_items(slots, source_items)
    # An entry resolves to one item; a second item with its ID (a duplicate) is tail but still agrees.
    entry_ids = {e.song_id for e in entries if e.song_id}
    entry_isrcs = {e.isrc for e in entries if e.isrc}
    cleared = []
    verb = "cleared" if apply else "would clear"
    for item in tail:
        if not item_spotify_ids(item):
            logger.info("  [KEEP] %s  (no Spotify ID: a local-only extra or unidentified, left tagged)", _describe(item))
            continue
        if item_spotify_ids(item) & entry_ids or item_isrcs(item) & entry_isrcs:
            logger.info("  [KEEP] %s  (an entry agrees by Spotify ID or ISRC but resolves to another item)", _describe(item))
            continue
        cleared.append(item)
        logger.info("  [UNTAG] %s  (sources=%s, no entry agrees by Spotify ID or ISRC)", _describe(item), _flex(item, "sources"))
        if apply:
            item["sources"] = ",".join(p.strip() for p in item.get("sources").split(",") if p.strip() and p.strip() != name)
            item.store()
    logger.info("%s: %d entries, %d tagged items, %d tail: %s %s's tag on %d, kept %d%s",
                name, len(entries), len(source_items), len(tail), verb, name, len(cleared), len(tail) - len(cleared),
                "" if apply else " (dry run; --apply to write)")
    return cleared


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-playlists", description=__doc__.splitlines()[0])
    parser.add_argument("--clear-tail", metavar="PLAYLIST",
                        help="clear PLAYLIST's tag on its tail items that carry a Spotify ID (#259; dry run by default)")
    parser.add_argument("--apply", action="store_true", help="with --clear-tail: write the changes")
    parser.add_argument("--albums", action="store_true",
                        help="album playlists: the .m3u counts before and after album-level listing (#268), read-only")
    args = parser.parse_args()
    if args.apply and not args.clear_tail:
        parser.error("--apply needs --clear-tail")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    if args.albums:
        album_audit()
        raise SystemExit(0)
    if args.clear_tail:
        from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
        from music_scan.scan import SPOTDL_DIR  # noqa: PLC0415

        with MusicLibrary(LIBRARY_DB) as lib:
            clear_tail(lib, args.clear_tail, SPOTDL_DIR, apply=args.apply)
        raise SystemExit(0)
    raise SystemExit(1 if audit() else 0)

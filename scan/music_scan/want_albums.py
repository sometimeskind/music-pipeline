"""music-audit-want-albums: the want list grouped by album (#322, homelab#2362).

The nightly downloads a spotdl playlist's want list (the store's entries no
library item holds by Spotify ID or ISRC, minus downloads held in the inbox
or quarantine) from the front, ``SYNC_TRACK_LIMIT`` tracks a night.  Before
big playlists switch to Usenet first, this audit shows how far whole-album
grabs would get them: for each spotdl playlist (every store without an
``.album`` or ``.nosync`` sentinel; ``--playlist`` for one, ``--all`` for
every store), the want list grouped by album, with the albums that hold at
least ``--min`` wanted tracks (default 2) listed most wanted first.

``--library`` (homelab#2403) is the baseline for a library-wide rule: every
library item by its ``spotify_album_id`` (one per recording) plus every
store's wanted tracks, the albums 2+ of them reach sorted into whole, album
playlist, queued, grab (a track wanted) and backfill (none wanted), and what
grabbing the last two whole would land and supersede.

Backoff is not applied: a track spotdl could not find is still wanted from
Usenet.  The tracks album mode holds off spotdl (#324) are left out, so the
report shows what is left for spotdl.  Read-only, and no Spotify or indexer call: the stores and the beets
library are enough.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
from pathlib import Path
from typing import Callable

from music_fetch import store
from music_fetch.albums import State, _spotify_album, _track_id, _wants, album_key
from music_scan.held import album_held, held_urls
from music_scan.identity import ItemIndex, item_recording_ids
from music_scan.library import LIBRARY_DB, MusicLibrary

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
QUARANTINE = Path("/root/Music/quarantine")
DEFAULT_MIN = 2


@dataclasses.dataclass
class AlbumWants:
    """One album's wanted tracks on a playlist."""

    key: str
    artist: str
    name: str
    tracks: int
    """The album's track count from the song record, 0 when unknown."""
    wanted: list[dict]
    first: int
    """Playlist position (0-based) of the album's first wanted track."""


@dataclasses.dataclass
class Report:
    """A playlist's want list by album."""

    playlist: str
    entries: int
    albums: list[AlbumWants]

    @property
    def wanted(self) -> int:
        return sum(len(a.wanted) for a in self.albums)

    def over(self, minimum: int) -> list[AlbumWants]:
        """The albums holding at least *minimum* wanted tracks, most wanted
        first, then the one whose first wanted track comes first."""
        return sorted((a for a in self.albums if len(a.wanted) >= minimum), key=lambda a: (-len(a.wanted), a.first))

    def covered(self, minimum: int) -> int:
        return sum(len(a.wanted) for a in self.over(minimum))

    @property
    def singletons(self) -> int:
        return sum(1 for a in self.albums if len(a.wanted) == 1)


def want_songs(songs: list[dict], resolves: Callable[[dict], bool], held: frozenset[str]) -> list[dict]:
    """The nightly's want list before backoff (:func:`store.want_list`), as
    song records in playlist order."""
    by_url = {song.get("url"): song for song in songs}
    return [by_url[url] for url in store.want_list(songs, resolves, held)]


def group(name: str, songs: list[dict], resolves: Callable[[dict], bool], held: frozenset[str]) -> Report:
    """Playlist *name*'s want list grouped by album (:func:`albums.album_key`)."""
    position = {}
    for pos, song in enumerate(songs):
        position.setdefault(song.get("url"), pos)
    albums: dict[str, AlbumWants] = {}
    for song in want_songs(songs, resolves, held):
        key = album_key(song)
        album = albums.get(key)
        if album is None:
            album = albums[key] = AlbumWants(
                key, song.get("album_artist") or (song.get("artists") or [""])[0], song.get("album_name", ""),
                int(song.get("tracks_count") or 0), [], position[song.get("url")],
            )
        album.wanted.append(song)
    return Report(name, len(songs), list(albums.values()))


def spotdl_playlists(spotdl_dir: Path = SPOTDL_DIR) -> list[str]:
    """The playlists with a store and neither an ``.album`` nor a ``.nosync`` sentinel."""
    return [n for n in store.names()
            if not (spotdl_dir / f"{n}.album").exists() and not (spotdl_dir / f"{n}.nosync").exists()]


def _summary(label: str, entries: int, albums: list[AlbumWants], minimum: int) -> str:
    wanted = sum(len(a.wanted) for a in albums)
    over = [a for a in albums if len(a.wanted) >= minimum]
    covered = sum(len(a.wanted) for a in over)
    share = f" ({covered * 100 // wanted}%)" if wanted else ""
    return (f"[WANT] {label}: {wanted} wanted of {entries} entr{'y' if entries == 1 else 'ies'} on {len(albums)} album(s); "
            f"{len(over)} album(s) hold {minimum}+ ({covered} wanted{share}), "
            f"{sum(1 for a in albums if len(a.wanted) == 1)} singleton(s)")


def merged(reports: list[Report]) -> list[AlbumWants]:
    """The albums of *reports* merged by key, each wanted track once, as the
    album tick counts them across playlists (``feed_wants``, homelab#2406)."""
    albums: dict[str, AlbumWants] = {}
    seen: dict[str, set[str]] = {}
    for report in reports:
        for album in report.albums:
            into = albums.setdefault(album.key, dataclasses.replace(album, wanted=[]))
            ids = seen.setdefault(album.key, set())
            for song in album.wanted:
                if _track_id(song) not in ids:
                    ids.add(_track_id(song))
                    into.wanted.append(song)
    return list(albums.values())


def log_reports(reports: list[Report], minimum: int = DEFAULT_MIN) -> None:
    """One ``[WANT]`` summary per playlist, its albums at or over *minimum*
    (``[ALBUM]``, most wanted first), then the total across playlists: the
    albums merged, each wanted track counted once (:func:`merged`), and the
    entries summed."""
    for report in reports:
        logger.info(_summary(report.playlist, report.entries, report.albums, minimum))
        for album in report.over(minimum):
            tracks = f" of {album.tracks}" if album.tracks else ""
            logger.info("  [ALBUM] %s: %d%s track(s) wanted, first at #%d: %s — %s (%s)",
                        report.playlist, len(album.wanted), tracks, album.first + 1, album.artist, album.name, album.key)
    if len(reports) > 1:
        logger.info(_summary("total", sum(r.entries for r in reports), merged(reports), minimum))


@dataclasses.dataclass
class LibraryAlbum:
    """One Spotify album's tracks across the library and every playlist's
    want list (``--library``, homelab#2403)."""

    key: str
    artist: str
    name: str
    tracks: int
    """Spotify's track count (a song record's, else the items' ``tracktotal``), 0 when unknown."""
    held: list = dataclasses.field(default_factory=list)
    """Library items, one per recording."""
    wanted: list[dict] = dataclasses.field(default_factory=list)
    record: dict | None = None
    """The album's record in the album store, if any."""
    on: dict[str, list[dict]] = dataclasses.field(default_factory=dict)
    """Every store track on the album, held or wanted, by playlist (:func:`library_view`)."""

    @property
    def refs(self) -> int:
        return len(self.held) + len(self.wanted)

    @property
    def loose(self) -> list:
        """The held items no release import placed (no ``album_record``):
        what supersede would replace after a whole-album grab."""
        return [i for i in self.held if not i.get("album_record")]

    @property
    def whole(self) -> bool:
        """The library holds Spotify's track count, or a release import placed it."""
        return bool(self.tracks and len(self.held) >= self.tracks) or len(self.loose) < len(self.held)

    @property
    def bucket(self) -> str:
        """``album playlist`` (a later/keep record has it), ``whole`` (the
        library holds Spotify's track count, or a release import placed it),
        ``queued`` (today's wants record has it), ``grab`` (2+ references
        with a wanted track) or ``backfill`` (2+ references, nothing wanted)."""
        if self.record and not _wants(self.record):
            return "album playlist"
        if self.whole:
            return "whole"
        if self.record:
            return "queued"
        return "grab" if self.wanted else "backfill"

    @property
    def to_land(self) -> int:
        """Tracks a whole-album grab would add, 0 when the track count is unknown."""
        return max(self.tracks - len(self.held), 0)


def library_albums(items, wanted: list[AlbumWants], records: dict[str, dict]) -> tuple[list[LibraryAlbum], int]:
    """Every Spotify album the library's items (by ``spotify_album_id``) and
    the *wanted* albums reference, each recording once, and the number of
    items with no Spotify album (not counted).  Local-file keys are left out."""
    albums: dict[str, LibraryAlbum] = {}
    seen: dict[str, set[str]] = {}
    unplaced = 0
    # A record by any of its Spotify album IDs: two editions of one release group are one (homelab#2410).
    records = {ref: r for k, r in records.items() for ref in (k, *(r.get("spotify_album_ids") or ()))}
    for item in items:
        key = item.get("spotify_album_id")
        if not key:
            unplaced += 1
            continue
        album = albums.get(key)
        if album is None:
            album = albums[key] = LibraryAlbum(key, item.get("albumartist") or item.get("artist") or "",
                                               item.get("album") or "", 0, record=records.get(key))
        album.tracks = max(album.tracks, int(item.get("tracktotal") or 0))
        ids = item_recording_ids(item) or frozenset({f"item:{item.id}"})
        known = seen.setdefault(key, set())
        if ids & known:
            continue  # another copy of a recording already counted
        known |= ids
        album.held.append(item)
    for want in wanted:
        if not _spotify_album(want.key):
            continue
        album = albums.get(want.key)
        if album is None:
            album = albums[want.key] = LibraryAlbum(want.key, want.artist, want.name, 0, record=records.get(want.key))
        if want.tracks:
            album.tracks = want.tracks  # Spotify's own count beats a tag
        album.wanted.extend(want.wanted)
    return list(albums.values()), unplaced


def library_view(items, stores: dict[str, list[dict]], resolves: Callable[[dict], bool],
                 records: dict[str, dict] | None = None) -> tuple[list[LibraryAlbum], int]:
    """:func:`library_albums` over *items* and every store's want list (no
    tracks held back for the inbox or album mode: those are references too),
    each album with every store track on it (:attr:`LibraryAlbum.on`).  The
    album tick's library feed reads the same view (homelab#2403)."""
    reports = [group(name, songs, resolves, frozenset()) for name, songs in stores.items()]
    albums, unplaced = library_albums(items, merged(reports), records or {})
    by_key = {a.key: a for a in albums}
    for name, songs in stores.items():
        for song in songs:
            if (album := by_key.get(album_key(song))) is not None:
                album.on.setdefault(name, []).append(song)
    return albums, unplaced


def _size(items) -> int:
    total = 0
    for item in items:
        try:
            total += os.path.getsize(os.fsdecode(item.path))
        except (OSError, TypeError):
            pass
    return total


def log_library(albums: list[LibraryAlbum], unplaced: int, minimum: int = DEFAULT_MIN) -> None:
    """The library-wide baseline: how many albums *minimum* references
    reach, by :attr:`LibraryAlbum.bucket`, and what grabbing the ``grab`` and
    ``backfill`` ones whole would land and supersede."""
    held = sum(len(a.held) for a in albums)
    logger.info("[LIBRARY] %d recording(s) held on %d Spotify album(s), %d wanted; %d item(s) with no Spotify album (not counted)",
                held, len(albums), sum(len(a.wanted) for a in albums), unplaced)
    over = [a for a in albums if a.refs >= minimum]
    buckets: dict[str, list[LibraryAlbum]] = {}
    for album in over:
        buckets.setdefault(album.bucket, []).append(album)
    counts = ", ".join(f"{b} {len(buckets.get(b, []))}" for b in ("whole", "album playlist", "queued", "grab", "backfill"))
    logger.info("[LIBRARY] %d album(s) with %d+ track(s): %s", len(over), minimum, counts)
    todo = buckets.get("grab", []) + buckets.get("backfill", [])
    if not todo:
        return
    landing = sum(a.to_land for a in todo)
    unknown = sum(1 for a in todo if not a.tracks)
    loose = sum(len(a.loose) for a in todo)
    per_track = _size(i for a in albums for i in a.held) / held if held else 0
    logger.info("[LIBRARY] grabbing those %d whole lands ~%d track(s) (~%.1f GB at %.1f MB a track; %d album(s) of unknown size), "
                "supersedes up to %d loose cop%s", len(todo), landing, landing * per_track / 1e9, per_track / 1e6,
                unknown, loose, "y" if loose == 1 else "ies")
    for album in sorted(todo, key=lambda a: (-a.refs, a.artist.casefold(), a.name.casefold())):
        tracks = f" of {album.tracks}" if album.tracks else ""
        logger.info("  [%s] %d held + %d wanted%s: %s — %s (%s)", album.bucket.upper(), len(album.held),
                    len(album.wanted), tracks, album.artist, album.name, album.key)


def run_library(minimum: int = DEFAULT_MIN) -> list[LibraryAlbum]:
    """``--library``: every library item and every store's want list (no
    tracks held back for the inbox or album mode: those are references
    too), by Spotify album."""
    try:
        records = State.load().albums
    except (OSError, ValueError):
        records = {}
    with MusicLibrary(LIBRARY_DB) as lib:
        items = list(lib.all_items())
        albums, unplaced = library_view(items, read_stores(), resolves_by(items), records)
        log_library(albums, unplaced, minimum)
    return albums


def resolves_by(items) -> Callable[[dict], bool]:
    """Whether a store song is in the library, by Spotify ID or ISRC (the nightly's test)."""
    index = ItemIndex(items)
    return lambda song: index.match_song(song, words=False)[0] is not None



def read_stores() -> dict[str, list[dict]]:
    """Every readable store's songs, by playlist."""
    stores = {}
    for name in store.names():
        playlist_store = store.load(name)
        if playlist_store is None:
            logger.error("[WANT] %s: no readable store", name)
            continue
        stores[name] = playlist_store.songs
    return stores


def run(playlist: str | None = None, all_stores: bool = False, minimum: int = DEFAULT_MIN) -> list[Report]:
    """Report the want list by album for *playlist*, every store (*all_stores*),
    or the spotdl playlists."""
    if playlist:
        names = [playlist]
    elif all_stores:
        names = store.names()
    else:
        names = spotdl_playlists(SPOTDL_DIR)
    with MusicLibrary(LIBRARY_DB) as lib:
        index = ItemIndex(lib.all_items())
    held = held_urls(QUARANTINE, SPOTDL_DIR) | album_held()
    reports: list[Report] = []
    for name in names:
        playlist_store = store.load(name)
        if playlist_store is None:
            logger.error("[WANT] %s: no readable store", name)
            continue
        reports.append(group(name, playlist_store.songs, lambda s: index.match_song(s, words=False)[0] is not None, held))
    log_reports(reports, minimum)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-want-albums", description=__doc__.splitlines()[0])
    parser.add_argument("--playlist", metavar="NAME", help="one playlist (default: every spotdl playlist)")
    parser.add_argument("--all", action="store_true", help="every store, album and nosync playlists included")
    parser.add_argument("--library", action="store_true",
                        help="library-wide baseline (homelab#2403): held and wanted tracks by Spotify album")
    parser.add_argument("--min", type=int, default=DEFAULT_MIN, metavar="N",
                        help="list albums with at least this many wanted tracks (default %(default)s)")
    args = parser.parse_args()
    if sum(map(bool, (args.playlist, args.all, args.library))) > 1:
        parser.error("--playlist, --all and --library are separate runs")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    if args.library:
        run_library(minimum=args.min)
    else:
        run(playlist=args.playlist, all_stores=args.all, minimum=args.min)

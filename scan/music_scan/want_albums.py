"""music-audit-want-albums: the want list grouped by album (#322, homelab#2362).

The nightly downloads a spotdl playlist's want list (the store's entries no
library item holds by Spotify ID or ISRC, minus downloads held in the inbox
or quarantine) from the front, ``SYNC_TRACK_LIMIT`` tracks a night.  Before
big playlists switch to Usenet first, this audit shows how far whole-album
grabs would get them: for each spotdl playlist (every store without an
``.album`` or ``.nosync`` sentinel; ``--playlist`` for one, ``--all`` for
every store), the want list grouped by album, with the albums that hold at
least ``--min`` wanted tracks (default 2) listed most wanted first.

Backoff is not applied: a track spotdl could not find is still wanted from
Usenet.  Read-only, and no Spotify or indexer call: the stores and the beets
library are enough.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path
from typing import Callable

from music_fetch import store
from music_fetch.albums import album_key
from music_scan.held import held_urls
from music_scan.identity import ItemIndex
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


def log_reports(reports: list[Report], minimum: int = DEFAULT_MIN) -> None:
    """One ``[WANT]`` summary per playlist, its albums at or over *minimum*
    (``[ALBUM]``, most wanted first), then the total across playlists."""
    for report in reports:
        logger.info(_summary(report.playlist, report.entries, report.albums, minimum))
        for album in report.over(minimum):
            tracks = f" of {album.tracks}" if album.tracks else ""
            logger.info("  [ALBUM] %s: %d%s track(s) wanted, first at #%d: %s — %s (%s)",
                        report.playlist, len(album.wanted), tracks, album.first + 1, album.artist, album.name, album.key)
    if len(reports) > 1:
        logger.info(_summary("total", sum(r.entries for r in reports), [a for r in reports for a in r.albums], minimum))


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
    held = held_urls(QUARANTINE, SPOTDL_DIR)
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
    parser.add_argument("--min", type=int, default=DEFAULT_MIN, metavar="N",
                        help="list albums with at least this many wanted tracks (default %(default)s)")
    args = parser.parse_args()
    if args.playlist and args.all:
        parser.error("--playlist and --all are separate runs")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(playlist=args.playlist, all_stores=args.all, minimum=args.min)

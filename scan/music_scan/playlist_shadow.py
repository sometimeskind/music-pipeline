"""``music-playlist-shadow``: compare each playlist's Navidrome playlist with
its store's Navidrome view, read-only (homelab#2361).

The two-way sync will read a Navidrome playlist, map each track to a beets
item by its path, and read every difference from the store's Navidrome view
(the entries that resolve to a library item, deduped, in store order: what
the ``.m3u`` holds) as a user edit.  Today every Navidrome playlist is
imported from the pipeline's ``.m3u``, so in a healthy state there is no
difference; any there is would be misread as a local add or delete.  This
check runs that read and mapping and reports the differences, before any
write path exists.

Per playlist with a store (``later``, the Later queue's, has none), the
Navidrome playlist imported from its ``.m3u`` (matched by file path under
``NAVIDROME_PLAYLISTS_DIR``, never by name) is read through the native API.
Each track's path (``libraryPath`` + ``path``) maps to a pipeline path by
swapping Navidrome's music folder (``NAVIDROME_MUSIC_DIR``, ``/music``) for
the pipeline's (``/root/Music``), and the path to a beets item.  Logged per
playlist, one ``[SHADOW]`` line:

- **view**: the store view's entries, then its local entries (the ``.m3u``
  lines the scan would write now);
- **navidrome**: the Navidrome playlist's tracks;
- **would add**: a track that maps to a beets item not in the view, which a
  two-way engine would read as an add;
- **would delete**: a view entry no track maps to, which it would read as a
  delete (the dangerous class);
- **unmapped**: a track whose path maps to no beets item;
- **missing**: a track Navidrome flags ``missing`` (its file is gone);
- **order**: whether the entries both sides hold are in the same order.

Each side is deduped by path, the first copy kept.  ``--verbose`` lists the
offending entries.  Nothing is written: no store save, no Navidrome write, no
event.  The run is skipped while Navidrome scans (songs are half-updated).
Exits 1 only when Navidrome can't be read; differences exit 0.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from music_fetch import later_queue, store
from music_scan import navidrome, playlists, scan
from music_scan.identity import ItemIndex
from music_scan.library import MusicLibrary

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Track:
    """One Navidrome playlist track: its pipeline *path* (None when it isn't
    under Navidrome's music folder), Navidrome's own path, and its labels."""

    path: Path | None
    nd_path: str
    artist: str = ""
    title: str = ""
    missing: bool = False

    def label(self) -> str:
        return f"{self.artist} — {self.title} ({self.nd_path})"


@dataclasses.dataclass
class Shadow:
    """One playlist's comparison.  *tracks* is the Navidrome playlist's
    track count, duplicates and unmapped ones included."""

    view: int
    tracks: int
    would_add: list[Track]
    would_delete: list[Path]
    unmapped: list[Track]
    missing: list[Track]
    duplicates: list[Track]
    order_same: bool
    first_moved: tuple[int, Path, Path] | None = None  # (position, view's entry, Navidrome's)

    @property
    def clean(self) -> bool:
        return not (self.would_add or self.would_delete or self.unmapped or self.missing or self.duplicates
                    or not self.order_same)


def pipeline_path(track: Mapping, nd_music: PurePosixPath, music: Path) -> Path | None:
    """The pipeline's path of a Navidrome playlist *track*: its ``libraryPath``
    joined with its ``path``, Navidrome's music folder *nd_music* swapped for
    the pipeline's *music*.  None when the track isn't under *nd_music*."""
    full = PurePosixPath(track.get("libraryPath") or "/") / (track.get("path") or "")
    try:
        return music / full.relative_to(nd_music)
    except ValueError:
        return None


def to_track(row: Mapping, nd_music: PurePosixPath, music: Path) -> Track:
    nd_path = str(PurePosixPath(row.get("libraryPath") or "/") / (row.get("path") or ""))
    return Track(pipeline_path(row, nd_music, music), nd_path, row.get("artist") or "", row.get("title") or "",
                 bool(row.get("missing")))


def diff(view: Iterable[Path], tracks: list[Track], items: Mapping[Path, object]) -> Shadow:
    """Compare a playlist's store *view* (paths in order) with its Navidrome
    *tracks* (in playlist order), each track mapped to a beets item through
    *items* (pipeline path → item).

    A track that maps to no item is ``unmapped`` and takes no further part; a
    second track on one path is a duplicate.  Of the rest, a track not in the
    view would read as an add, and a view entry no track holds as a delete.
    The order is compared over the paths both sides hold."""
    view = list(dict.fromkeys(view))
    in_view = set(view)
    missing = [t for t in tracks if t.missing]
    unmapped: list[Track] = []
    duplicates: list[Track] = []
    held: dict[Path, Track] = {}
    for t in tracks:
        if t.path is None or t.path not in items:
            unmapped.append(t)
        elif t.path in held:
            duplicates.append(t)
        else:
            held[t.path] = t
    would_add = [t for p, t in held.items() if p not in in_view]
    would_delete = [p for p in view if p not in held]
    ours = [p for p in view if p in held]
    theirs = [p for p in held if p in in_view]
    first_moved = next(((i, a, b) for i, (a, b) in enumerate(zip(ours, theirs), 1) if a != b), None)
    return Shadow(len(view), len(tracks), would_add, would_delete, unmapped, missing, duplicates,
                  first_moved is None, first_moved)


def local_tail(name: str, paths: list[Path], library: ItemIndex) -> list[Path]:
    """Playlist *name*'s local entries after its view *paths*, as
    :func:`scan._local_tail` lists them, without its save: each local entry
    whose item is in the library and not in the view, in store order."""
    playlist = store.load(name)
    if playlist is None:
        return []
    by_id = {item.id: item for item in library.items}
    matched = set(paths)
    out: list[Path] = []
    for entry in playlist.locals:
        if entry.get("id") in by_id and (p := scan._item_path(by_id[entry["id"]])) not in matched:
            matched.add(p)
            out.append(p)
    return out


def _nd_playlists(found: list[dict], nd_dir: PurePosixPath) -> dict[str, dict]:
    """The Navidrome playlists imported from a file in *nd_dir*, by that file's
    stem; of several from one file, the first with sync on."""
    by_stem: dict[str, list[dict]] = {}
    for pls in found:
        if pls.get("path") and PurePosixPath(pls["path"]).parent == nd_dir:
            by_stem.setdefault(PurePosixPath(pls["path"]).stem, []).append(pls)
    out = {}
    for stem, candidates in by_stem.items():
        if len(candidates) > 1:
            logger.warning("[SHADOW] %s: %d Navidrome playlists are imported from %s.m3u; compared with the first "
                           "with sync on", stem, len(candidates), stem)
        out[stem] = next((p for p in candidates if p.get("sync")), candidates[0])
    return out


def _log(name: str, shadow: Shadow, items: Mapping[Path, object]) -> None:
    logger.info("[SHADOW] %s: view %d, navidrome %d, would add %d, would delete %d, unmapped %d, missing %d, "
                "order %s", name, shadow.view, shadow.tracks, len(shadow.would_add), len(shadow.would_delete),
                len(shadow.unmapped), len(shadow.missing), "same" if shadow.order_same else "differs")
    for t in shadow.would_add:
        logger.debug("[SHADOW]   %s would add: %s", name, t.label())
    for p in shadow.would_delete:
        item = items.get(p)
        logger.debug("[SHADOW]   %s would delete: %s — %s (%s)", name, getattr(item, "artist", ""),
                     getattr(item, "title", ""), p)
    for t in shadow.unmapped:
        logger.debug("[SHADOW]   %s unmapped: %s → %s", name, t.label(), t.path or "outside the music folder")
    for t in shadow.missing:
        logger.debug("[SHADOW]   %s missing in Navidrome: %s", name, t.label())
    for t in shadow.duplicates:
        logger.debug("[SHADOW]   %s duplicate in Navidrome: %s", name, t.label())
    if shadow.first_moved:
        i, ours, theirs = shadow.first_moved
        logger.debug("[SHADOW]   %s first order difference at shared entry %d: view %s, navidrome %s",
                     name, i, ours, theirs)


def run(names: list[str] | None = None) -> int:
    """Compare playlists *names* (every store's when None); returns the exit status."""
    config = navidrome._config()
    if config is None:
        logger.error("[SHADOW] NAVIDROME_URL is unset: nothing to compare with")
        return 1
    url, user, password = config
    nd_dir = PurePosixPath(os.environ.get("NAVIDROME_PLAYLISTS_DIR") or navidrome.DEFAULT_PLAYLISTS_DIR)
    nd_music = PurePosixPath(os.environ.get("NAVIDROME_MUSIC_DIR") or navidrome.DEFAULT_MUSIC_DIR)
    music = scan.LIBRARY.parent
    try:
        if navidrome.scanning():
            logger.info("[SHADOW] Navidrome is scanning, so its songs are half-updated: skipped, run it again later")
            return 0
        headers = navidrome._login(url, user, password)
        found = _nd_playlists(navidrome.list_playlists(url, headers), nd_dir)
    except RuntimeError as exc:
        logger.error("[SHADOW] %s", exc)
        return 1

    stores = [n for n in store.names() if n != later_queue.PLAYLIST]
    if names:
        for n in sorted(set(names) - set(stores)):
            logger.warning("[SHADOW] %s: no playlist store%s; skipped", n,
                           " (the Later queue writes later.m3u)" if n == later_queue.PLAYLIST else "")
        stores = [n for n in stores if n in names]

    status, clean, differ, absent = 0, 0, 0, 0
    with MusicLibrary(scan.LIBRARY_DB) as lib:
        all_items = lib.all_items()
        library = ItemIndex(all_items)
        by_album = playlists.by_album_id(all_items)
        stamps = playlists.record_stamps(scan._album_records())
        items = {scan._item_path(item): item for item in all_items}
        for name in stores:
            try:
                view = scan.store_view(name, library, by_album, stamps)
                if view is None:
                    continue
                paths = view[0] + local_tail(name, view[0], library)
            except Exception:
                logger.error("[SHADOW] %s: its store view failed; not compared", name, exc_info=True)
                continue
            pls = found.get(name)
            if pls is None:
                logger.info("[SHADOW] %s: view %d, no Navidrome playlist imported from %s", name, len(paths),
                            nd_dir / f"{name}.m3u")
                absent += 1
                continue
            if not pls.get("sync"):
                logger.warning("[SHADOW] %s: sync is off in Navidrome, so it no longer follows %s", name, pls["path"])
            try:
                rows = navidrome.playlist_tracks(url, headers, pls["id"])
            except RuntimeError as exc:
                logger.error("[SHADOW] %s: %s", name, exc)
                status = 1
                continue
            shadow = diff(paths, [to_track(r, nd_music, music) for r in rows], items)
            _log(name, shadow, items)
            clean, differ = clean + shadow.clean, differ + (not shadow.clean)

    try:
        if navidrome.scanning():
            logger.warning("[SHADOW] Navidrome started scanning during the run: differences may be half-updated "
                           "songs; run it again")
    except RuntimeError as exc:
        logger.warning("[SHADOW] %s", exc)
    logger.info("[SHADOW] %d playlist(s): %d the same, %d differ, %d with no Navidrome playlist", clean + differ + absent,
                clean, differ, absent)
    return status


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-playlist-shadow",
        description="Compare each playlist's Navidrome playlist with its store's Navidrome view, read-only "
                    "(homelab#2361)")
    parser.add_argument("playlists", nargs="*", help="the playlists to compare (default: every store's)")
    parser.add_argument("-v", "--verbose", action="store_true", help="list the offending entries")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    if args.verbose:
        for noisy in ("urllib3", "requests", "psycopg", "beets"):
            logging.getLogger(noisy).setLevel(logging.INFO)
    sys.exit(run(args.playlists or None))

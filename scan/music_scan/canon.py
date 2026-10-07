"""Canonical album tags from Spotify (#209).

Every import is a singleton filed under ``$albumartist/$album``, so one album
splits in Navidrome when its tracks come from different editions or sources.
Spotify is authoritative for album grouping (homelab#1889): an item with a
Spotify ID takes ``album``, ``albumartist``, the release date and its
track/disc numbering from the Spotify album its playlist entry is on, and beets
moves the file to match.  Items with no Spotify ID, or whose IDs no playlist
entry describes (``[NOALBUM]``), are left alone.

The album data comes from the playlist item pages, as the ``.spotdl`` snapshots
store them: no Spotify calls at import.  The pages carry no disc count, so
``disctotal`` is the highest disc number seen for the album unless an older,
fully fetched entry has ``disc_count``.

Release preference, for an item whose ``spotify_ids`` are on several releases:
``album`` over ``single`` over ``compilation``; then a release a playlist entry
names today over one known only from an old snapshot; then the earliest
release; then the album ID, so every run agrees.

A track whose chosen release is a single moves to a standard album (``album``,
never ``compilation``) by the same album artist that holds the same recording,
found by ISRC with a Spotify track search (:class:`AlbumsByIsrc`).  An EP (Spotify
files EPs as ``single`` too: a ``single`` of at least :data:`EP_MIN_TRACKS`
tracks) counts only when the item is already filed under it, so Ice Spice *Munch*
stays on *Like..? (Deluxe)* but no track moves onto a remix EP or a reissue.
Several candidates: an album before an EP, then the one the item is already filed
under, then one a playlist names, then the earliest, then the album ID.  The searches are cached in
:data:`ISRC_CACHE` and paced :data:`SPOTIFY_INTERVAL` apart; a search that is
not done yet (budget spent, rate limit) leaves the item as it is and marks it
``canon_wait`` so the next scan picks it up.

Navidrome groups by the MusicBrainz album ID before the tags, so the album-level
MusicBrainz tags follow the canonical album too (:mod:`music_scan.mb_release`);
``mb_album_via`` records how (``url``, ``isrc``, ``upc``, ``none`` or ``pending``).
Embedded art follows the canonical album when the album name or album artist
changes, or when the file has none.  A change to beets-only fields
(``spotify_album_id``, ``mb_album_via``) updates the database and leaves the
file alone, so Navidrome has nothing to rescan.

At import the scan canonicalises new items and those still pending; the album
completion does it after ``tag_album_ids``.  ``music-canon-albums`` backfills
the library: dry run by default, ``--apply`` writes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_scan.identity import item_spotify_ids, spotify_id
from music_scan.mb_release import MB_ALBUM_FIELDS, Resolver, SpotifyAlbum

logger = logging.getLogger(__name__)

TYPE_RANK = {"album": 0, "single": 1, "compilation": 2}
# The backfill's page reads, reused by an --apply within a day of the dry run.
PAGES_CACHE = Path("/root/Music/inbox/spotdl/.canon-pages.json")
PAGES_MAX_AGE = 24 * 3600
# MusicBrainz lookups per scan run; the rest wait in the queue for the next.
IMPORT_BUDGET = 20
PENDING = "pending"
# Single → album searches by ISRC: cache, retry for a miss, pacing, import budget.
ISRC_CACHE = Path("/root/Music/inbox/spotdl/.canon-isrc.json")
ISRC_RETRY_DAYS = 7
SPOTIFY_INTERVAL = 1.0
IMPORT_SEARCH_BUDGET = 20
# A Spotify "single" with this many tracks is an EP, and a single's track may move onto it.
EP_MIN_TRACKS = 4
# Bumped when what the cache keeps changes, so older entries are searched again.
ISRC_CACHE_VERSION = 2
WAIT = "canon_wait"
SPOTIFY_FIELDS = ("album", "albumartist", "year", "month", "day", "track", "tracktotal", "disc", "disctotal")


@dataclasses.dataclass(frozen=True)
class Release:
    album_id: str
    name: str
    artist: str
    album_type: str
    date: str
    tracks_count: int
    disc_count: int
    cover_url: str | None
    isrcs: tuple[str, ...]

    def spotify_album(self) -> SpotifyAlbum:
        return SpotifyAlbum(self.album_id, self.name, self.tracks_count, self.isrcs)


@dataclasses.dataclass(frozen=True)
class Placement:
    """One Spotify track on its release."""

    release: Release
    track: int
    disc: int
    named: bool  # a playlist entry names it today
    isrc: str = ""

    def key(self):
        r = self.release
        return (TYPE_RANK.get(r.album_type, len(TYPE_RANK)), not self.named, r.date or "9999", r.album_id)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def placements_from(sources: Iterable[tuple[list[dict], bool]]) -> dict[str, Placement]:
    """``{spotify track id: Placement}`` from ``.spotdl``-format songs.

    *sources* are ``(songs, named)`` pairs; the first one to place a track wins,
    so pass current playlist pages before old snapshots.
    """
    sources = [(list(songs), named) for songs, named in sources]
    albums: dict[str, dict] = {}
    for songs, _ in sources:
        for song in songs:
            album_id = song.get("album_id")
            if not album_id or not song.get("album_name"):
                continue
            a = albums.setdefault(album_id, {"song": song, "discs": 0, "isrcs": {}})
            a["discs"] = max(a["discs"], _int(song.get("disc_count")), _int(song.get("disc_number")))
            if song.get("isrc"):
                a["isrcs"].setdefault(song["isrc"], None)
    releases = {
        album_id: Release(
            album_id=album_id,
            name=a["song"]["album_name"],
            artist=a["song"].get("album_artist") or a["song"].get("artist") or "",
            album_type=a["song"].get("album_type") or "",
            date=a["song"].get("date") or str(a["song"].get("year") or ""),
            tracks_count=_int(a["song"].get("tracks_count")),
            disc_count=max(a["discs"], 1),
            cover_url=a["song"].get("cover_url") if str(a["song"].get("cover_url", "")).startswith("https://") else None,
            isrcs=tuple(a["isrcs"]),
        )
        for album_id, a in albums.items()
    }
    placed: dict[str, Placement] = {}
    for songs, named in sources:
        for song in songs:
            sid = song.get("song_id") or spotify_id(song.get("url"))
            release = releases.get(song.get("album_id"))
            if sid and release and sid not in placed:
                placed[sid] = Placement(release, _int(song.get("track_number")), _int(song.get("disc_number")) or 1,
                                        named, song.get("isrc") or "")
    return placed


def spotdl_songs(spotdl_dir: Path) -> list[dict]:
    songs: list[dict] = []
    for f in sorted(spotdl_dir.glob("*.spotdl")):
        try:
            songs.extend(json.loads(f.read_text(encoding="utf-8")).get("songs", []))
        except Exception:
            continue
    return songs


def choose(item, placements: dict[str, Placement]) -> Placement | None:
    """The item's canonical release (see the module docstring), or None."""
    found = [placements[s] for s in item_spotify_ids(item) if s in placements]
    return min(found, key=Placement.key) if found else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _release_from_track(track: dict) -> tuple[Release, int, int]:
    """A Spotify track object (as a search returns it) → its release, track and disc number."""
    album = track.get("album") or {}
    images = sorted(album.get("images") or [], key=lambda i: -(i.get("width") or 0))
    cover = images[0]["url"] if images and str(images[0].get("url", "")).startswith("https://") else None
    disc = _int(track.get("disc_number")) or 1
    release = Release(
        album_id=album.get("id", ""),
        name=album.get("name", ""),
        artist=((album.get("artists") or [{}])[0]).get("name", ""),
        album_type=album.get("album_type", ""),
        date=album.get("release_date", ""),
        tracks_count=_int(album.get("total_tracks")),
        disc_count=disc,
        cover_url=cover,
        isrcs=tuple(i for i in [(track.get("external_ids") or {}).get("isrc")] if i),
    )
    return release, _int(track.get("track_number")), disc


def _album_or_ep(album: dict) -> bool:
    kind = album.get("album_type")
    return kind == "album" or (kind == "single" and _int(album.get("total_tracks")) >= EP_MIN_TRACKS)


def spotify_search_isrc(isrc: str) -> list[dict]:
    """Spotify's tracks with *isrc*.  One call, rate-limit guarded."""
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415
    from spotdl.utils.spotify import SpotifyClient  # noqa: PLC0415

    SpotifyPlaylists(ingest.COOKIE_FILE)  # initialises the shared client with the fail-fast adapter
    return ((SpotifyClient().search(q=f"isrc:{isrc}", type="track", limit=50) or {}).get("tracks") or {}).get("items") or []


class AlbumsByIsrc:
    """ISRC → the standard albums holding that recording, through a cache.

    Searches are paced :data:`SPOTIFY_INTERVAL` apart and capped at *budget*
    per run; a Spotify rate limit stops searching for the rest of the run."""

    def __init__(
        self,
        cache_file: Path | None = ISRC_CACHE,
        budget: int | None = None,
        search: Callable[[str], list[dict]] = spotify_search_isrc,
        interval: float = SPOTIFY_INTERVAL,
    ) -> None:
        self.cache_file = cache_file
        self.budget = budget
        self.search = search
        self.interval = interval
        self.calls = 0
        self.limited = False
        self._last = 0.0
        self.cache: dict[str, dict] = {}
        if cache_file is not None:
            try:
                self.cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                logger.warning("Ignoring unreadable ISRC album cache %s", cache_file)

    def save(self) -> None:
        if self.cache_file is None:
            return
        try:
            self.cache_file.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save the ISRC album cache %s: %s", self.cache_file, exc)

    def _due(self, entry: dict | None) -> bool:
        if entry is None or entry.get("v") != ISRC_CACHE_VERSION:
            return True
        return not entry["tracks"] and datetime.fromisoformat(entry["checked"]) < _now() - timedelta(days=ISRC_RETRY_DAYS)

    def tracks(self, isrc: str) -> list[dict] | None:
        """The album tracks with *isrc* (Spotify track objects), or None while not searched yet."""
        from music_fetch.spotify_limit import SpotifyRateLimited  # noqa: PLC0415

        entry = self.cache.get(isrc)
        if self._due(entry):
            if entry is not None and entry.get("v") != ISRC_CACHE_VERSION:
                entry = None  # kept less than this version needs
            if self.limited or (self.budget is not None and self.calls >= self.budget):
                return entry["tracks"] if entry else None
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.calls += 1
            try:
                found = self.search(isrc)
            except SpotifyRateLimited as exc:
                self.limited = True
                logger.warning("  [SP-WAIT] Spotify is rate-limiting, no more ISRC searches this run: %s", exc)
                return entry["tracks"] if entry else None
            except Exception as exc:
                logger.warning("  [SP-WAIT] ISRC %s: Spotify search failed, retried next run: %s", isrc, exc)
                return entry["tracks"] if entry else None
            finally:
                self._last = time.monotonic()
            # Keep only what a Release needs: albums and EPs carrying this exact ISRC.
            keep = [
                {"album": {k: t["album"].get(k) for k in ("id", "name", "album_type", "artists", "release_date",
                                                          "total_tracks", "images")},
                 "track_number": t.get("track_number"), "disc_number": t.get("disc_number"),
                 "external_ids": {"isrc": isrc}}
                for t in found
                if _album_or_ep(t.get("album") or {}) and t["album"].get("id")
                and ((t.get("external_ids") or {}).get("isrc") or "").upper() == isrc.upper()
            ]
            entry = self.cache[isrc] = {"v": ISRC_CACHE_VERSION, "checked": _now().replace(microsecond=0).isoformat(),
                                        "tracks": keep}
        return entry["tracks"]

    def album_for(self, item, p: Placement, placements: dict[str, Placement]) -> Placement | None | bool:
        """The album placement replacing single placement *p*: a Placement, None
        when there is none, or False while the search is still to do."""
        found = self.tracks(p.isrc)
        if found is None:
            return False
        artist = p.release.artist.casefold()
        named = {q.release.album_id: q for q in placements.values()}
        current = str(item.get("album") or "").casefold()
        options = []
        for t in found:
            release, track, disc = _release_from_track(t)
            if release.artist.casefold() != artist or release.album_id == p.release.album_id:
                continue
            if release.album_type != "album" and (release.name.casefold() != current
                                                  or release.tracks_count <= p.release.tracks_count):
                continue  # an EP only when the item is already filed under it
            known = named.get(release.album_id)
            if known is not None:
                release = known.release  # the page's data (disc count, ISRCs) over the search's
            options.append(Placement(release, track, disc, known is not None and known.named, p.isrc))

        def key(q: Placement):
            return (q.release.album_type != "album", q.release.name.casefold() != current, not q.named,
                    q.release.date or "9999", q.release.album_id)

        return min(options, key=key) if options else None


def spotify_fields(p: Placement) -> dict:
    parts = [_int(x) for x in p.release.date.split("-")[:3]] + [0, 0, 0]
    return {
        "album": p.release.name,
        "albumartist": p.release.artist,
        "year": parts[0],
        "month": parts[1],
        "day": parts[2],
        "track": p.track,
        "tracktotal": p.release.tracks_count,
        "disc": p.disc,
        "disctotal": max(p.release.disc_count, p.disc),
    }


def _path(item) -> str:
    return os.fsdecode(item.path)


@dataclasses.dataclass
class Change:
    item: object
    placement: Placement
    diff: dict  # field → (old, new)
    dest: str
    art: bool
    write: bool  # the file changes: a tag it stores, or the cover
    source: str = ""  # the path when planned; dest differs when the file moves


@dataclasses.dataclass
class Plan:
    changes: list[Change] = dataclasses.field(default_factory=list)
    scanned: int = 0
    with_id: int = 0
    noalbum: int = 0
    unchanged: int = 0
    clashes: int = 0
    singles: int = 0  # items placed on their album instead of a single (whether or not they change)
    waiting: int = 0  # items left until their single's ISRC search is done
    kinds: Counter = dataclasses.field(default_factory=Counter)
    # Spotify album ID → the (albumartist, album) names its items carry today,
    # changed or not, so the report can say how many names become one album.
    names: dict[str, set] = dataclasses.field(default_factory=dict)

    @property
    def moved(self) -> int:
        # Against the planned path: after --apply the item is already at dest (#218).
        return sum(1 for c in self.changes if c.dest != c.source)

    @property
    def art(self) -> int:
        return sum(1 for c in self.changes if c.art)

    @property
    def writes(self) -> int:
        return sum(1 for c in self.changes if c.write)


def _renamed(diff: dict) -> bool:
    return "album" in diff or "albumartist" in diff


def _kind(diff: dict, write: bool) -> str:
    if _renamed(diff):
        return "album"
    if any(f in diff for f in ("year", "month", "day")):
        return "date"
    if any(f in diff for f in ("track", "tracktotal", "disc", "disctotal")):
        return "numbering"
    return "musicbrainz" if write else "beets-only"


def plan_changes(
    items: Iterable,
    placements: dict[str, Placement],
    resolver: Resolver | None,
    albums: AlbumsByIsrc | None = None,
) -> Plan:
    """What canonicalising *items* would change.  Leaves the items as they were."""
    from beets.library import Item  # noqa: PLC0415

    from music_scan.cover import has_art  # noqa: PLC0415

    plan = Plan()
    dests: dict[str, object] = {}
    for item in items:
        plan.scanned += 1
        if not item_spotify_ids(item):
            continue
        plan.with_id += 1
        p = choose(item, placements)
        if p is None:
            plan.noalbum += 1
            logger.debug("  [NOALBUM] %s: no playlist entry describes its Spotify IDs", _path(item))
            continue
        if albums is not None and p.release.album_type == "single" and p.isrc:
            album = albums.album_for(item, p, placements)
            if album is False:
                plan.waiting += 1
                here = _path(item)
                plan.changes.append(Change(item, p, {WAIT: (item.get(WAIT), "1")}, here, False, False, here))
                continue
            if album is not None:
                logger.debug("  [ALBUM] %s: single %s → album %s by ISRC %s", _path(item), p.release.name,
                             album.release.name, p.isrc)
                p = album
                plan.singles += 1
        fields = spotify_fields(p)
        mb = resolver.fields(p.release.spotify_album()) if resolver is not None else None
        if mb is None:
            fields["mb_album_via"] = PENDING
        else:
            fields.update(mb)
            fields["mb_album_via"] = resolver.cache[p.release.album_id]["rung"]
        fields["spotify_album_id"] = p.release.album_id
        if item.get(WAIT):
            fields[WAIT] = None
        plan.names.setdefault(p.release.album_id, set()).add((str(item.get("albumartist") or ""),
                                                               str(item.get("album") or "")))
        diff = {k: (item.get(k), v) for k, v in fields.items() if item.get(k) != v}
        art = bool(p.release.cover_url) and (_renamed(diff) or not has_art(item))
        write = art or any(k in Item._media_fields for k in diff)
        if not diff and not art:
            plan.unchanged += 1
            continue
        old = {k: item.get(k) for k in diff}
        for k, (_, v) in diff.items():
            item[k] = v
        dest = os.fsdecode(item.destination())
        for k, v in old.items():
            item[k] = v
        if dest != _path(item) and (dest in dests or os.path.exists(dest)):
            # Moving would overwrite another file: leave this item as it is (a duplicate, #210).
            plan.clashes += 1
            logger.warning("  [CLASH] %s → %s: another item already has that path; left as it is",
                           _path(item), dest)
            continue
        dests[dest] = item
        plan.changes.append(Change(item, p, diff, dest, art, write, _path(item)))
        plan.kinds[_kind(diff, write)] += 1
    return plan


def _show(v) -> str:
    return repr(v) if isinstance(v, str) else str(v)


def log_change(change: Change, apply: bool) -> None:
    if WAIT in change.diff and change.diff[WAIT][1]:
        logger.info("  [WAIT] %s %s: single %s, its ISRC %s not searched yet%s", change.item.id,
                    _path(change.item), change.placement.release.name, change.placement.isrc,
                    "" if apply else " (dry run)")
        return
    shown = [f"{k} {_show(o)}→{_show(n)}" for k, (o, n) in change.diff.items()
             if k in SPOTIFY_FIELDS or k == "mb_album_via" or k == "mb_albumid"]
    if change.art:
        shown.append("cover")
    where = f" → {change.dest}" if change.dest != _path(change.item) else ""
    db_only = "" if change.write else " (database only)"
    logger.info("  [RETAG] %s %s: %s%s%s%s", change.item.id, _path(change.item), ", ".join(shown),
                where, db_only, "" if apply else " (dry run)")


def apply_change(change: Change, images: dict[str, bytes | None], fetch: Callable[[str], bytes]) -> bool:
    """Write the new tags (and cover) to the file, move it, store the item.

    A change to beets-only fields only stores the item.  When the cover can't
    be downloaded nothing is written, so the next run retries the whole change
    (once the album is renamed the cover would no longer be due)."""
    from beets.util import MoveOperation  # noqa: PLC0415

    from music_scan import cover  # noqa: PLC0415

    item = change.item
    url = change.placement.release.cover_url
    if change.art and url:
        if url not in images:
            try:
                images[url] = fetch(url)
            except Exception as exc:
                logger.warning("  [NOART] %s: cover download failed (%s): %s", change.placement.release.name, url, exc)
                images[url] = None
        if images[url] is None:
            raise OSError("the cover download failed")
    for k, (_, v) in change.diff.items():
        if v is None:
            del item[k]
        else:
            item[k] = v
    if change.write:
        if not item.try_write():
            # Storing tags the file doesn't have would be reverted by the scan's beet update.
            raise OSError("could not write the tags")
        if change.art and url:
            try:
                cover.embed(item, images[url])
            except Exception as exc:
                logger.warning("  [NOART] %s: embedding failed: %s", _path(item), exc)
    if change.dest != _path(item):
        item.move(operation=MoveOperation.MOVE, store=False)
    item.store()
    return True


def canonicalize(
    items: Iterable,
    placements: dict[str, Placement],
    resolver: Resolver | None,
    apply: bool = True,
    fetch: Callable[[str], bytes] | None = None,
    albums: AlbumsByIsrc | None = None,
) -> Plan:
    """Plan, log and (with *apply*) write the canonical tags on *items*."""
    from music_scan import cover  # noqa: PLC0415

    fetch = fetch or cover.download
    plan = plan_changes(items, placements, resolver, albums)
    images: dict[str, bytes | None] = {}
    for change in plan.changes:
        log_change(change, apply)
        if apply:
            try:
                apply_change(change, images, fetch)
            except Exception as exc:
                logger.warning("  [RETAG] %s: failed, retried next run: %s", _path(change.item), exc)
    if resolver is not None:
        resolver.save()
    if albums is not None:
        albums.save()
    return plan


def canonicalize_items(items: list, spotdl_dir: Path | None = None, budget: int | None = IMPORT_BUDGET) -> int:
    """The import hook: canonicalise *items* from the ``.spotdl`` snapshots."""
    from music_fetch import ingest  # noqa: PLC0415

    if not items:
        return 0
    placements = placements_from([(spotdl_songs(spotdl_dir or ingest.SPOTDL_DIR), True)])
    plan = canonicalize(items, placements, Resolver(budget=budget), albums=AlbumsByIsrc(budget=IMPORT_SEARCH_BUDGET))
    if plan.changes:
        logger.info("Canonical albums: retagged %d item(s), moved %d, %d waiting for an ISRC search",
                    len(plan.changes) - plan.waiting, plan.moved, plan.waiting)
    return len(plan.changes) - plan.waiting


def after_scan(lib, since: float) -> int:
    """Items the scan just imported, plus those whose MusicBrainz release is still
    pending or missing (the queue and its weekly retry) or whose single's ISRC
    search is still to do."""
    items = [
        i for i in lib.all_items()
        if (i.added or 0) >= since or i.get("mb_album_via") in (PENDING, "none") or i.get(WAIT)
    ]
    return canonicalize_items(items)


# ----------------------------------------------------------------------
# Backfill
# ----------------------------------------------------------------------


def read_pages(refresh: bool = False, cache_file: Path = PAGES_CACHE) -> dict[str, list[dict]]:
    """Every configured playlist's songs from Spotify's item pages (one call per
    100 tracks), cached for a day so a dry run and its --apply read Spotify once."""
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.config import load_playlists  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415

    if not refresh:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cached["read_at"] < PAGES_MAX_AGE:
                logger.info("Using the playlist pages read %s ago", f"{(time.time() - cached['read_at']) / 60:.0f} min")
                return cached["playlists"]
        except (OSError, ValueError, KeyError):
            pass
    reader = SpotifyPlaylists(ingest.COOKIE_FILE)
    pages: dict[str, list[dict]] = {}
    for pl in load_playlists(ingest.CONF_PATH):
        pages[pl.name] = reader.songs(pl.url)
        logger.info("Read %s: %d track(s)", pl.name, len(pages[pl.name]))
    try:
        cache_file.write_text(json.dumps({"read_at": time.time(), "playlists": pages}), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not cache the playlist pages in %s: %s", cache_file, exc)
    return pages


def report(plan: Plan, resolver: Resolver | None, albums: AlbumsByIsrc | None = None) -> None:
    by_album: dict[str, list[Change]] = {}
    for c in plan.changes:
        if WAIT in c.diff and c.diff[WAIT][1]:
            continue
        by_album.setdefault(c.placement.release.album_id, []).append(c)
    for album_id, changes in by_album.items():
        r = changes[0].placement.release
        old = sorted({album or "?" for _, album in plan.names.get(album_id, ())})
        tag = {"single": "[SINGLE]", "compilation": "[COMP]"}.get(r.album_type, "[ALBUM]")
        logger.info("%s %s — %s (%s, %s, %s): %d item(s) from %d current album name(s): %s",
                    tag, r.artist, r.name, r.date or "?", r.album_type or "?", r.album_id,
                    len(changes), len(old), "; ".join(old))
    logger.info("Items: %d scanned, %d with a Spotify ID, %d with no album data (NOALBUM), %d unchanged",
                plan.scanned, plan.with_id, plan.noalbum, plan.unchanged)
    changes = len(plan.changes) - plan.waiting
    logger.info("Changes: %d item(s) (%s); %d file write(s), %d database-only; %d file(s) move, "
                "%d cover(s) replaced, %d clash(es)",
                changes, ", ".join(f"{k} {v}" for k, v in sorted(plan.kinds.items())) or "none",
                plan.writes, changes - plan.writes, plan.moved, plan.art, plan.clashes)
    renamed = {c.placement.release.album_id for c in plan.changes if _renamed(c.diff)}
    logger.info("Albums: %d current album name(s) become %d Spotify album(s)",
                sum(len(plan.names[a]) for a in renamed), len(renamed))
    if albums is not None:
        logger.info("Singles: %d item(s) on their album instead of a single, by ISRC; Spotify: %d ISRC search(es) "
                    "this run (%.0fs apart%s), %d ISRC(s) cached, %d item(s) wait for a search",
                    plan.singles, albums.calls, albums.interval, ", stopped by a rate limit" if albums.limited else "",
                    len(albums.cache), plan.waiting)
    if resolver is not None:
        logger.info("MusicBrainz: looked up %d album(s) this run (%s), %d call(s); %d album(s) cached",
                    resolver.looked_up, ", ".join(f"{k} {v}" for k, v in sorted(resolver.rungs.items())) or "none",
                    resolver.mb.calls, len(resolver.cache))


def run(apply: bool = False, refresh: bool = False, mb_budget: int | None = None,
        search_budget: int | None = None) -> Plan:
    from music_fetch import ingest  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, LIBRARY_DIR, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    from music_scan.scan import regen_playlists  # noqa: PLC0415

    pages = read_pages(refresh)
    placements = placements_from(
        [(songs, True) for songs in pages.values()] + [(spotdl_songs(ingest.SPOTDL_DIR), False)]
    )
    logger.info("%d Spotify track(s) placed on %d release(s)", len(placements),
                len({p.release.album_id for p in placements.values()}))
    resolver = Resolver(budget=mb_budget)
    albums = AlbumsByIsrc(budget=search_budget)
    with MusicLibrary(LIBRARY_DB, LIBRARY_DIR) as lib:
        plan = canonicalize(lib.all_items(), placements, resolver, apply=apply, albums=albums)
    report(plan, resolver, albums)
    changed = len(plan.changes) - plan.waiting
    if not apply:
        logger.info("Dry run: %d item(s) would change. Re-run with --apply to write.", changed)
        return plan
    logger.info("Retagged %d item(s)", changed)
    if plan.moved:
        # Navidrome drops .m3u entries whose file moved, so the playlists go first (#218).
        regen_playlists()
    if plan.writes:
        trigger_scan()
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-canon-albums", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the tags and move the files (default: dry run)")
    parser.add_argument("--refresh", action="store_true", help="re-read the playlist pages even if cached")
    parser.add_argument("--mb-budget", type=int, default=None, metavar="N",
                        help="look up at most N albums on MusicBrainz this run (default: all)")
    parser.add_argument("--search-budget", type=int, default=None, metavar="N",
                        help="at most N Spotify ISRC searches for single tracks this run (default: all, "
                             f"{SPOTIFY_INTERVAL:g}s apart)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply, refresh=args.refresh, mb_budget=args.mb_budget, search_budget=args.search_budget)

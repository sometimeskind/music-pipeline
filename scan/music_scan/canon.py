"""Canonical album tags from Spotify (#209).

Every import is a singleton filed under ``$albumartist/$album``, so one album
splits in Navidrome when its tracks come from different editions or sources.
Spotify is authoritative for album grouping (homelab#1889): an item with a
Spotify ID takes ``album``, ``albumartist``, the release date and its
track/disc numbering from the Spotify album its playlist entry is on, and beets
moves the file to match.  Items with no Spotify ID, or whose IDs no playlist
entry describes (``[NOALBUM]``), are left alone.

The album data comes from the playlist item pages, as the playlist stores
keep them: no Spotify calls at import.  The pages carry no disc count, so
``disctotal`` is the highest disc number seen for the album unless an older,
fully fetched entry has ``disc_count``.

Release preference, for an item whose ``spotify_ids`` are on several releases:
its album record's Spotify album, for an item a Usenet release imported
(:func:`record_homes`, #278); then ``album`` over ``single`` over
``compilation``; then a release a playlist entry names today over one known
only from a playlist store; then the earliest release; then the album ID, so
every run agrees.

A Usenet release stays one album (#278): an item on its record's Spotify album
stays there, whatever else its IDs are on.  It skips the single and twin steps
below, so its ``[RETAG]`` never changes ``spotify_album_id`` away from the
record's.  An item canon moved off before this goes back (``[HOME]``).  Loose
spotdl tracks keep the rules below.

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

Spotify sometimes lists one album twice (a reissue, a market copy): same name,
same album artist, two album IDs (#214).  Releases of the same type with the
same normalised name and album artist form a twin group (albums and EPs only;
a different name, like a deluxe edition, stays separate), and one of them
represents it: the one most playlist entries are on today, then the earliest,
then the one with most tracks, then the album ID.  An item placed on another
twin moves to the representative once the recording is known to be on it: a
playlist entry on the representative carries the item's ISRC, or an ISRC search
(the same :class:`AlbumsByIsrc` searches) lists it.  Its numbering comes from
that entry or hit; the date and cover are the representative's.  Same-name
albums sharing no recording (self-titled records) never merge.

Navidrome groups by the MusicBrainz album ID before the tags, so the album-level
MusicBrainz tags follow the canonical album too (:mod:`music_scan.mb_release`);
``mb_album_via`` records how (``url``, ``isrc``, ``upc``, ``none`` or ``pending``).
A twin group is looked up once, for its representative, and the URL rung tries
every twin's Spotify URL: MusicBrainz may link only the other release (#229).
Embedded art follows the canonical album when the album name or album artist
changes, or when the file has none.  A change to beets-only fields
(``spotify_album_id``, ``mb_album_via``) updates the database and leaves the
file alone, so Navidrome has nothing to rescan.

At import the scan canonicalises new items and those still pending; the album
completion does it after ``tag_album_ids``.  The scan also takes any item with
a Spotify ID and no ``spotify_album_id``: one that got its ID after its import
(a ``music-rollback-releases`` link, a duplicate's merge, #244).

The downloaded release is the album (#266, #279).  Its items carry
``album_record`` (:mod:`music_scan.release`), and each takes its record's
album, whatever its Spotify track IDs say or whether it has any: the album,
album artist, date and cover of the record's Spotify album, the MusicBrainz
album tags of the release the files are (``mb_album_via`` ``files``), and its
own disc/track.  Release items imported before #279 have no ``album_record``;
they keep #278's rule above.  A local-only extra (``album_extra``) takes them
too, the release's ``mb_albumid`` included, although its file is not from that
release (#306): Navidrome groups by the MusicBrainz album ID, so an extra
without it would show as an album of its own.  The album tags say which album
an item is filed under; its recording IDs (``mb_trackid``, ISRC, Spotify IDs)
stay its own, and ``album_extra`` marks it.

When an album record's item would move onto a path another item holds (a
legacy extra of the same track number and title), it takes the album's tags
where it is, logged ``[CLASH] … tagged in place``, so it stays on the album
rather than showing as a stray one, and is marked ``canon_clash``: every scan
tries the move again until the path is free (#306).  Spotify IDs come only from ISRC or disc/track
(``tag_album_ids``), never from a title.

The album copy then supersedes loose items that predate it
(:mod:`music_scan.supersede`).

``music-canon-albums`` backfills the library, supersedes included: dry run by
default, ``--apply`` writes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import time
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_fetch import events, state
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
# An album record's item tagged where it is: its album path was another item's (#306).
CLASH = "canon_clash"
# Seconds between two items of one release's import, at most (#278): a track takes ~10 s to convert.
IMPORT_GAP = 600
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

    def spotify_album(self, twins: Iterable[str] = ()) -> SpotifyAlbum:
        return SpotifyAlbum(self.album_id, self.name, self.tracks_count, self.isrcs, tuple(twins))


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
    """``{spotify track id: Placement}`` from spotdl-format songs.

    *sources* are ``(songs, named)`` pairs; the first one to place a track wins,
    so pass current playlist pages before the stores.
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
            cover_url=(a["song"].get("cover_url")
                       if str(a["song"].get("cover_url", "")).startswith("https://") else None),
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


def store_songs() -> list[dict]:
    """Every playlist store's Spotify song records (homelab#1914)."""
    from music_fetch import store  # noqa: PLC0415

    return [song for songs in store.songs_by_playlist().values() for song in songs]


def choose(item, placements: dict[str, Placement], homes: Iterable[str] = ()) -> Placement | None:
    """The item's canonical release (see the module docstring), or None.

    *homes* are the album records the item belongs to (:func:`record_homes`):
    a placement on one of them wins over any other preference (#278)."""
    found = [placements[s] for s in item_spotify_ids(item) if s in placements]
    homes = set(homes)
    home = [p for p in found if p.release.album_id in homes]
    return min(home or found, key=Placement.key) if found else None


def record_homes(items: Iterable, records: dict[str, dict], by_time: bool = False) -> dict[int, frozenset[str]]:
    """``{item id: album record keys}`` for the usenet *items* a release imported (#278).

    An item belongs to the records that claim it by album name and artist, as
    ``music-rollback-releases`` attributes it
    (:func:`music_scan.rollback.failed_release_items`).  With *by_time* (the
    backfill), an item added while a record's release was importing (that
    record's own items were added both before and after it) belongs to that
    record instead (neighbours within :data:`IMPORT_GAP`): canon may already
    have renamed it to another album, which the name no longer claims (3092,
    *1979*, moved to a best-of).  Imports run one at a time, so two releases'
    items don't interleave."""
    from music_scan.rollback import claimants  # noqa: PLC0415

    usenet = [i for i in items if i.get("via") == "usenet"]
    claiming = claimants(records)
    claimed = {i.id: frozenset(claiming(i)) for i in usenet}
    times: dict[str, list[float]] = {}
    if by_time:
        for i in usenet:
            for k in claimed[i.id]:
                times.setdefault(k, []).append(i.added or 0)
        for t in times.values():
            t.sort()
    homes: dict[int, frozenset[str]] = {}
    for i in usenet:
        t = i.added or 0
        mine = claimed[i.id]
        covering = frozenset(k for k, ts in times.items() if _inside(ts, t))
        if covering or mine:
            homes[i.id] = covering or mine
    return homes


def _inside(ts: list[float], t: float) -> bool:
    """True when sorted *ts* hold a time strictly before *t* and one strictly
    after it, each within :data:`IMPORT_GAP`: *t* is inside one import (the
    item's own time doesn't count).  The gap keeps a retried album's two
    releases, days apart, from enclosing another record's import."""
    before, after = bisect_left(ts, t), bisect_right(ts, t)
    return before > 0 and after < len(ts) and t - ts[before - 1] <= IMPORT_GAP and ts[after] - t <= IMPORT_GAP


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


def _is_album_or_ep(kind: str | None, tracks: int) -> bool:
    return kind == "album" or (kind == "single" and tracks >= EP_MIN_TRACKS)


def _album_or_ep(album: dict) -> bool:
    return _is_album_or_ep(album.get("album_type"), _int(album.get("total_tracks")))


# ----------------------------------------------------------------------
# Twin releases (#214)
# ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Twins:
    """Same-name releases of one album: the representative (ISRCs of the whole
    group) and the others, with the playlist entries on each today."""

    representative: Release
    others: tuple[Release, ...]
    entries: dict[str, int]  # album ID → named placements

    @property
    def releases(self) -> tuple[Release, ...]:
        return (self.representative, *self.others)


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def twin_groups(placements: dict[str, Placement]) -> dict[str, Twins]:
    """``{album ID: Twins}`` for every release in a same-name group (albums and EPs)."""
    entries = Counter(p.release.album_id for p in placements.values() if p.named)
    releases = {p.release.album_id: p.release for p in placements.values()}
    by_name: dict[tuple, list[Release]] = {}
    for r in releases.values():
        if _is_album_or_ep(r.album_type, r.tracks_count):
            by_name.setdefault((r.album_type, _norm(r.artist), _norm(r.name)), []).append(r)
    groups: dict[str, Twins] = {}
    for twins in by_name.values():
        if len(twins) < 2:
            continue
        twins.sort(key=lambda r: (-entries[r.album_id], r.date or "9999", -r.tracks_count, r.album_id))
        isrcs = tuple(dict.fromkeys(i for r in twins for i in r.isrcs))
        group = Twins(dataclasses.replace(twins[0], isrcs=isrcs), tuple(twins[1:]),
                      {r.album_id: entries[r.album_id] for r in twins})
        for r in twins:
            groups[r.album_id] = group
    return groups


def _on_representative(p: Placement, group: Twins, by_isrc: dict[tuple[str, str], Placement]) -> Placement | None:
    """*p* moved onto the group's representative by a playlist entry there with its ISRC, or None."""
    rep = group.representative
    q = by_isrc.get((rep.album_id, p.isrc.upper())) if p.isrc else None
    return dataclasses.replace(q, release=rep, isrc=p.isrc) if q is not None else None


def spotify_search_isrc(isrc: str) -> list[dict]:
    """Spotify's tracks with *isrc*.  One call, rate-limit guarded."""
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415
    from spotdl.utils.spotify import SpotifyClient  # noqa: PLC0415

    SpotifyPlaylists(ingest.COOKIE_FILE)  # initialises the shared client with the fail-fast adapter
    found = SpotifyClient().search(q=f"isrc:{isrc}", type="track", limit=50) or {}
    return (found.get("tracks") or {}).get("items") or []


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
        # The music database's canon-isrc document once the state is there (homelab#2410).
        self._store = None if cache_file is None else state.KeyedCache(state.CANON_ISRC, cache_file)
        self.cache: dict[str, dict] = {} if self._store is None else self._store.load()

    def save(self) -> None:
        if self._store is not None:
            self._store.save(self.cache)

    def _due(self, entry: dict | None) -> bool:
        if entry is None or entry.get("v") != ISRC_CACHE_VERSION:
            return True
        retry_after = _now() - timedelta(days=ISRC_RETRY_DAYS)
        return not entry["tracks"] and datetime.fromisoformat(entry["checked"]) < retry_after

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

    def twin_for(self, p: Placement, group: Twins) -> Placement | None | bool:
        """The placement on the group's representative replacing *p*, which is on
        another twin: a Placement when an ISRC search lists the recording on the
        representative, None when it does not, or False while the search is still to do."""
        rep = group.representative
        found = self.tracks(p.isrc)
        if found is None:
            return False
        for t in found:
            if (t.get("album") or {}).get("id") == rep.album_id:
                return Placement(rep, _int(t.get("track_number")), _int(t.get("disc_number")) or 1,
                                 group.entries.get(rep.album_id, 0) > 0, p.isrc)
        return None


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
    waiting: int = 0  # items left until an ISRC search is done (a single's album, or a twin)
    twins: dict[str, "Twins"] = dataclasses.field(default_factory=dict)  # album ID → its twin group
    on_twin: Counter = dataclasses.field(default_factory=Counter)  # album ID → items placed on it by their entry
    twinned: int = 0  # items moved from another twin onto the representative
    apart: int = 0  # items left on their twin: the recording is not on the representative
    twin_waiting: int = 0  # of waiting, those waiting for a twin's ISRC search
    kinds: Counter = dataclasses.field(default_factory=Counter)
    # Spotify album ID → the (albumartist, album) names its items carry today,
    # changed or not, so the report can say how many names become one album.
    names: dict[str, set] = dataclasses.field(default_factory=dict)
    homed: int = 0  # album-record items moved back onto their record's album from another (#278)
    records: int = 0  # items an album record's release names (#279)
    supersedes: list = dataclasses.field(default_factory=list)  # supersede.Pair: loose copies replaced (#266)

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
    homes: dict[int, frozenset[str]] | None = None,
    records: dict[str, dict] | None = None,
) -> Plan:
    """What canonicalising *items* would change.  Leaves the items as they were.

    An item stamped with its album record (*records*, #279) takes the record's
    album (:func:`_plan_record_item`).  For older release items, *homes*
    (:func:`record_homes`) keep them on their album record's album (#278)."""
    plan = Plan(twins=twin_groups(placements))
    # (album ID, ISRC) → a placement on that twin, to confirm a twin without a search.
    by_isrc = {(q.release.album_id, q.isrc.upper()): q for q in placements.values()
               if q.isrc and q.release.album_id in plan.twins}
    dests: dict[str, object] = {}
    releases = {q.release.album_id: q.release for q in placements.values()}
    items = list(items)
    # An album record's highest disc among these items (homelab#2491).
    discs: Counter[str] = Counter()
    for item in items:
        if (key := item.get("album_record")) and key in (records or {}):
            discs[key] = max(discs[key], _int(item.disc))
    for item in items:
        plan.scanned += 1
        key = item.get("album_record")
        if key and key in (records or {}):
            _plan_record_item(item, key, records[key], releases, resolver, plan, dests, discs[key])
            continue
        if not item_spotify_ids(item):
            continue
        plan.with_id += 1
        p = choose(item, placements, (homes or {}).get(item.id, ()))
        if p is None:
            plan.noalbum += 1
            logger.debug("  [NOALBUM] %s: no playlist entry describes its Spotify IDs", _path(item))
            continue
        # On its album record's album: no single or twin step moves it off (#278).
        home = p.release.album_id in (homes or {}).get(item.id, ())
        if home and item.get("spotify_album_id") not in (None, "", p.release.album_id):
            plan.homed += 1
            logger.info("  [HOME] %s %s: on %s, but a track of its album record's %s (%s); goes back",
                        item.id, _path(item), item.get("spotify_album_id"), p.release.name, p.release.album_id)
        wait = False
        if albums is not None and not home and p.release.album_type == "single" and p.isrc:
            album = albums.album_for(item, p, placements)
            if album is False:
                wait = True
            elif album is not None:
                logger.debug("  [ALBUM] %s: single %s → album %s by ISRC %s", _path(item), p.release.name,
                             album.release.name, p.isrc)
                p = album
                plan.singles += 1
        group = plan.twins.get(p.release.album_id) if not wait and not home else None
        if group is not None:
            plan.on_twin[p.release.album_id] += 1
            if p.release.album_id == group.representative.album_id:
                p = dataclasses.replace(p, release=group.representative)  # the group's ISRCs
            else:
                twin = _on_representative(p, group, by_isrc)
                if twin is None and albums is not None and p.isrc:
                    twin = albums.twin_for(p, group)
                if twin is False:
                    wait = True
                    plan.twin_waiting += 1
                elif twin is None:
                    plan.apart += 1
                    logger.info("  [NOTWIN] %s: %s (%s) stays apart from %s: ISRC %s is not on it", _path(item),
                                p.release.name, p.release.album_id, group.representative.album_id, p.isrc or "?")
                else:
                    logger.debug("  [TWIN] %s: %s → %s by ISRC %s", _path(item), p.release.album_id,
                                 twin.release.album_id, p.isrc)
                    p = twin
                    plan.twinned += 1
        if wait:
            plan.waiting += 1
            here = _path(item)
            plan.changes.append(Change(item, p, {WAIT: (item.get(WAIT), "1")}, here, False, False, here))
            continue
        fields = spotify_fields(p)
        # The representative's lookup tries its twins' URLs too; an item left apart keeps to its own release.
        twins = ([r.album_id for r in group.others]
                 if group is not None and p.release.album_id == group.representative.album_id else ())
        mb = resolver.fields(p.release.spotify_album(twins)) if resolver is not None else None
        if mb is None:
            fields["mb_album_via"] = PENDING
        else:
            fields.update(mb)
            fields["mb_album_via"] = resolver.cache[p.release.album_id]["rung"]
        fields["spotify_album_id"] = p.release.album_id
        _plan_fields(item, p, fields, plan, dests)
    return plan


def _plan_record_item(item, key: str, record: dict, releases: dict[str, Release], resolver: Resolver | None,
                      plan: Plan, dests: dict, discs: int = 0) -> None:
    """Plan an album record's item (#279): the record's Spotify album names it
    (album, album artist, date, cover, ``spotify_album_id``), the files' own
    MusicBrainz release gives its MusicBrainz album tags, and it keeps its own
    disc/track: the release is the album.  Its track IDs play no part.

    A singleton import leaves ``disctotal`` 0, so a multi-disc release's discs
    shared file names and clashed (homelab#2491).  *discs*, the record's highest
    disc among the items canonicalised with it, sets ``disctotal`` when it is 2
    or more, and never lowers it: the path's disc prefix then applies."""
    release = releases.get(key)
    if release is None:
        plan.noalbum += 1
        logger.debug("  [NOALBUM] %s: no playlist entry describes its album record %s", _path(item), key)
        return
    p = Placement(release, item.track or 0, item.disc or 1, True)
    fields = {k: v for k, v in spotify_fields(p).items() if k in ("album", "albumartist", "year", "month", "day")}
    if discs >= 2:
        fields["disctotal"] = max(_int(item.disctotal), discs)
    mb = (record.get("release_files") or {}).get("mb")
    if mb:
        fields.update(mb)
        fields["mb_album_via"] = "files"
    else:
        found = resolver.fields(release.spotify_album()) if resolver is not None else None
        if found is None:
            fields["mb_album_via"] = PENDING
        else:
            fields.update(found)
            fields["mb_album_via"] = resolver.cache[key]["rung"]
    fields["spotify_album_id"] = key
    plan.records += 1
    _plan_fields(item, p, fields, plan, dests, in_place=True)


def _clash_event(item, dest: str, message: str, in_place: bool) -> None:
    """A clash stands until someone fixes it, and every scan sees it again: one
    event a day per clash (homelab#2413)."""
    events.record("clash", message, source="canon",
                  spotify_album_id=events.spotify_album(item.get("album_record") or item.get("spotify_album_id")),
                  release_group=item.get("mb_releasegroupid"), item_id=item.id,
                  data={**events.item_fields(item), "path": _path(item), "dest": dest, "in_place": in_place},
                  once_hours=24)


def _plan_fields(item, p: Placement, fields: dict, plan: Plan, dests: dict, in_place: bool = False) -> None:
    """Add the change *fields* make to *item* to *plan*, unless it changes
    nothing.  One that would move onto another file is left out, or with
    *in_place* (an album record's item, #306) tagged where it is."""
    from beets.library import Item  # noqa: PLC0415

    from music_scan.cover import has_art  # noqa: PLC0415

    if item.get(WAIT):
        fields[WAIT] = None
    if item.get(CLASH):
        fields[CLASH] = None
    plan.names.setdefault(p.release.album_id, set()).add((str(item.get("albumartist") or ""),
                                                           str(item.get("album") or "")))
    diff = {k: (item.get(k), v) for k, v in fields.items() if item.get(k) != v}
    art = bool(p.release.cover_url) and (_renamed(diff) or not has_art(item))
    write = art or any(k in Item._media_fields for k in diff)
    if not diff and not art:
        plan.unchanged += 1
        return
    old = {k: item.get(k) for k in diff}
    for k, (_, v) in diff.items():
        item[k] = v
    dest = os.fsdecode(item.destination())
    for k, v in old.items():
        item[k] = v
    if dest != _path(item) and (dest in dests or os.path.exists(dest)):
        plan.clashes += 1
        if not in_place:
            # Moving would overwrite another file: leave this item as it is (a duplicate, #210).
            logger.warning("  [CLASH] %s → %s: another item already has that path; left as it is",
                           _path(item), dest)
            _clash_event(item, dest, f"[CLASH] {_path(item)} → {dest}: another item already has that path; "
                                     "left as it is", in_place=False)
            return
        # The release's track stays on its album, where it is; the next scan tries the move again.
        logger.warning("  [CLASH] %s %s → %s: another item already has that path; tagged in place",
                       item.id, _path(item), dest)
        _clash_event(item, dest, f"[CLASH] {item.id} {_path(item)} → {dest}: another item already has that path; "
                                 "tagged in place", in_place=True)
        fields[CLASH] = "1"
        diff = {k: (item.get(k), v) for k, v in fields.items() if item.get(k) != v}
        if not diff and not art:
            return
        dest = _path(item)
        write = art or any(k in Item._media_fields for k in diff)
    dests[dest] = item
    plan.changes.append(Change(item, p, diff, dest, art, write, _path(item)))
    plan.kinds[_kind(diff, write)] += 1


def _show(v) -> str:
    return repr(v) if isinstance(v, str) else str(v)


def log_change(change: Change, apply: bool) -> None:
    if WAIT in change.diff and change.diff[WAIT][1]:
        r = change.placement.release
        logger.info("  [WAIT] %s %s: %s %s, its ISRC %s not searched yet%s", change.item.id, _path(change.item),
                    "single" if r.album_type == "single" else f"twin release {r.album_id} of", r.name,
                    change.placement.isrc, "" if apply else " (dry run)")
        return
    shown = [f"{k} {_show(o)}→{_show(n)}" for k, (o, n) in change.diff.items()
             if k in SPOTIFY_FIELDS or k in ("spotify_album_id", "mb_album_via", "mb_albumid")]
    if change.art:
        shown.append("cover")
    where = f" → {change.dest}" if change.dest != _path(change.item) else ""
    db_only = "" if change.write else " (database only)"
    logger.info("  [RETAG] %s %s: %s%s%s%s", change.item.id, _path(change.item), ", ".join(shown),
                where, db_only, "" if apply else " (dry run)")


def _moved_event(change: Change) -> None:
    """A ``moved`` event when canon moved an item off one Spotify album onto
    another (homelab#2413): the digest flags a move between unrelated albums."""
    old_sid, new_sid = change.diff.get("spotify_album_id", (None, None))
    if not old_sid or old_sid == new_sid:
        return
    item = change.item
    old_album, new_album = change.diff.get("album", (item.get("album"), item.get("album")))
    events.record("moved", f"[RETAG] {item.id} {change.source or _path(item)}: "
                           f"album {old_album!r}→{new_album!r}, spotify_album_id {old_sid!r}→{new_sid!r}",
                  source="canon", spotify_album_id=events.spotify_album(new_sid),
                  release_group=item.get("mb_releasegroupid"), item_id=item.id,
                  data={**events.item_fields(item), "from": {"album": old_album, "spotify_album_id": old_sid},
                        "to": {"album": new_album, "spotify_album_id": new_sid}})


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


class Phases:
    """Seconds per phase of one canon run, logged as one line (homelab#2491)."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}
        self._start = self._last = time.monotonic()

    def lap(self, name: str) -> None:
        """The time since the previous lap goes to *name*."""
        now = time.monotonic()
        self.seconds[name] = self.seconds.get(name, 0.0) + now - self._last
        self._last = now

    def log(self, what: str) -> None:
        logger.info("Canon phases (%s) in %.1fs: %s", what, time.monotonic() - self._start,
                    ", ".join(f"{name} {s:.1f}s" for name, s in self.seconds.items()))


def canonicalize(
    items: Iterable,
    placements: dict[str, Placement],
    resolver: Resolver | None,
    apply: bool = True,
    fetch: Callable[[str], bytes] | None = None,
    albums: AlbumsByIsrc | None = None,
    homes: dict[int, frozenset[str]] | None = None,
    records: dict[str, dict] | None = None,
    phases: Phases | None = None,
) -> Plan:
    """Plan, log and (with *apply*) write the canonical tags on *items*."""
    from music_scan import cover  # noqa: PLC0415

    phases = phases or Phases()
    fetch = fetch or cover.download
    plan = plan_changes(items, placements, resolver, albums, homes, records)
    phases.lap("plan")
    images: dict[str, bytes | None] = {}
    for change in plan.changes:
        log_change(change, apply)
        if apply:
            try:
                if apply_change(change, images, fetch):
                    _moved_event(change)
            except Exception as exc:
                logger.warning("  [RETAG] %s: failed, retried next run: %s", _path(change.item), exc)
    phases.lap("write")
    if resolver is not None:
        resolver.save()
    if albums is not None:
        albums.save()
    phases.lap("save")
    return plan


def _records() -> dict[str, dict]:
    """The album records, or none when the state can't be read (canon goes on without homes)."""
    from music_fetch import albums  # noqa: PLC0415

    try:
        return albums.State.load(albums.STATE_FILE).albums
    except (OSError, ValueError) as exc:
        logger.warning("Could not read the album records (%s); no item kept on its record's album", exc)
        return {}


def canonicalize_items(items: list, songs: list[dict] | None = None, budget: int | None = IMPORT_BUDGET,
                       records: dict[str, dict] | None = None, phases: Phases | None = None,
                       library: int | None = None) -> int:
    """The import hook: canonicalise *items* from the playlist stores' *songs*
    (:func:`store_songs` when not given).  A release's items stay on their
    album record's album (*records*, read from the album state when not given;
    #278).  Logs the time per phase (*phases*, continued from the item
    selection when given; *library* is how many items it chose from)."""
    if not items:
        return 0
    phases = phases or Phases()
    songs = store_songs() if songs is None else songs
    phases.lap("stores")
    placements = placements_from([(songs, True)])
    phases.lap("placements")
    records = _records() if records is None else records
    phases.lap("records")
    resolver, albums = Resolver(budget=budget), AlbumsByIsrc(budget=IMPORT_SEARCH_BUDGET)
    phases.lap("caches")
    homes = record_homes(items, records)
    phases.lap("homes")
    plan = canonicalize(items, placements, resolver, albums=albums, homes=homes, records=records, phases=phases)
    if plan.changes:
        logger.info("Canonical albums: retagged %d item(s), moved %d, %d waiting for an ISRC search",
                    len(plan.changes) - plan.waiting, plan.moved, plan.waiting)
    phases.log(f"{len(items)} item(s)" + (f" of {library}" if library is not None else ""))
    return len(plan.changes) - plan.waiting


def after_scan(lib, since: float) -> int:
    """Items the scan just imported, plus those whose MusicBrainz release is still
    pending or missing (the queue and its weekly retry), whose single's ISRC
    search is still to do, or that got a Spotify ID after their import (#244)."""
    phases = Phases()
    everything = lib.all_items()
    phases.lap("library")
    items = [
        i for i in everything
        if (i.added or 0) >= since or i.get("mb_album_via") in (PENDING, "none") or i.get(WAIT) or i.get(CLASH)
        or (not i.get("spotify_album_id") and item_spotify_ids(i))
    ]
    phases.lap("select")
    return canonicalize_items(items, phases=phases, library=len(everything))


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
            cached = state.load_doc(state.CANON_PAGES, cache_file)
            if cached and time.time() - cached["read_at"] < PAGES_MAX_AGE:
                logger.info("Using the playlist pages read %s ago", f"{(time.time() - cached['read_at']) / 60:.0f} min")
                return cached["playlists"]
        except (OSError, ValueError, KeyError, state.Unavailable):
            pass
    reader = SpotifyPlaylists(ingest.COOKIE_FILE)
    pages: dict[str, list[dict]] = {}
    for pl in load_playlists(ingest.CONF_PATH):
        pages[pl.name] = reader.songs(pl.url)
        logger.info("Read %s: %d track(s)", pl.name, len(pages[pl.name]))
    try:
        state.save_doc(state.CANON_PAGES, cache_file, {"read_at": time.time(), "playlists": pages})
    except (OSError, state.Unavailable) as exc:
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
    logger.info("Album records: %d release item(s) on another album go back to their record's album (HOME)",
                plan.homed)
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
    groups = {id(g): g for g in plan.twins.values()}
    for g in sorted(groups.values(), key=lambda g: (g.representative.artist, g.representative.name)):
        shown = [f"{r.album_id} ({r.date or '?'}, {r.tracks_count} tracks, {g.entries.get(r.album_id, 0)} entries, "
                 f"{plan.on_twin.get(r.album_id, 0)} items)" for r in g.releases]
        logger.info("[TWIN] %s — %s: %s ← %s", g.representative.artist, g.representative.name, shown[0],
                    "; ".join(shown[1:]))
    logger.info("Twins: %d group(s) of same-name releases; %d item(s) moved onto the representative, "
                "%d wait for an ISRC search, %d left apart (recording not on it)",
                len(groups), plan.twinned, plan.twin_waiting, plan.apart)
    if resolver is not None:
        logger.info("MusicBrainz: looked up %d album(s) this run (%s), %d call(s); %d album(s) cached",
                    resolver.looked_up, ", ".join(f"{k} {v}" for k, v in sorted(resolver.rungs.items())) or "none",
                    resolver.mb.calls, len(resolver.cache))


def run(apply: bool = False, refresh: bool = False, mb_budget: int | None = None,
        search_budget: int | None = None) -> Plan:
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.albums import STATE_FILE, State  # noqa: PLC0415
    from music_scan import supersede  # noqa: PLC0415
    from music_scan.audit import pipeline_lock  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, LIBRARY_DIR, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    from music_scan.scan import regen_playlists  # noqa: PLC0415

    pages = read_pages(refresh)
    placements = placements_from(
        [(songs, True) for songs in pages.values()] + [(store_songs(), False)]
    )
    logger.info("%d Spotify track(s) placed on %d release(s)", len(placements),
                len({p.release.album_id for p in placements.values()}))
    resolver = Resolver(budget=mb_budget)
    albums = AlbumsByIsrc(budget=search_budget)
    # --apply moves and merges files (#266): no import may run meanwhile.
    with (pipeline_lock() if apply else contextlib.nullcontext()), MusicLibrary(LIBRARY_DB, LIBRARY_DIR) as lib:
        records = State.load(STATE_FILE).albums
        # Items canon moved off their record's album before #278 go back: by import time too.
        homes = record_homes(lib.all_items(), records, by_time=True)
        plan = canonicalize(lib.all_items(), placements, resolver, apply=apply, albums=albums, homes=homes,
                            records=records)
        plan.supersedes = supersede.backfill(lib, records, apply=apply)
    report(plan, resolver, albums)
    moved = plan.moved
    superseded = sum(len(p.loose) for p in plan.supersedes)
    logger.info("Supersede: %d loose item(s) superseded by their album copy", superseded)
    retagged = len(plan.changes) - plan.waiting
    changed = retagged + superseded
    # The same count as the Changes and Supersede lines above, not a third one.
    parts = f"{retagged} retagged, {superseded} superseded"
    if not apply:
        logger.info("Dry run: %d item(s) would change (%s). Re-run with --apply to write.", changed, parts)
        return plan
    logger.info("Changed %d item(s) (%s)", changed, parts)
    if moved or superseded:
        # Navidrome drops .m3u entries whose file moved, so the playlists go first (#218).
        counts = regen_playlists()
        logger.info("Moved %d file(s); playlists regenerated: %s", moved,
                    ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none")
    if plan.writes or superseded:
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

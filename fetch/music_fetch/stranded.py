"""Album rows stranded by a playlist that left ``playlists.conf`` (homelab#2602).

An album playlist's row keeps its tracks under the playlist's name.  When the
playlist leaves ``playlists.conf`` (``later`` and ``keep`` did at the stage 3
cutover, homelab#2478) nothing reads those tracks any more: the nightly
downloads fallback tracks only for configured playlists, :func:`albums._due`
searches a fallback again only for a request, and :func:`albums.feed_requests`
clears a dead playlist's tracks only on a requested row.  A row Usenet had
missed or failed on, with no request, then sat in ``fallback`` for good.

Each album tick in ``on`` (:func:`albums.tick`, right after
:func:`later_requests.tick`, so the requests it adds are fed the same tick)
therefore settles every *stranded* row: an album playlist's row (no feed's,
no request's) that no configured playlist lists, in ``missing``, ``failed``
or ``fallback``:

- **on Later** (a queue entry names it, by release group or by ref): the Later
  queue owns it, and :mod:`music_fetch.later_requests` requests it once it has
  a release group.  A ``fallback`` row goes back to ``missing``, its dead
  fallback tracks dropped, so album mode searches it on Usenet weekly
  meanwhile (a ``status_changed`` event).  One without a release group is
  looked up below every tick, which retries a miss weekly.
- **with a release group** and no request: requested, ``requested_by``
  :data:`BY`, so the request's MusicBrainz tracklist and fallback take over
  (the cutover did the same for Keep's albums that had one).  A release group
  MusicBrainz gives no tracklist for is dropped as below.
- **never looked up** (``unresolved: pending``: an album with no items, which
  canon never looks up): its Spotify album is looked up in MusicBrainz by URL,
  ISRC and barcode (:mod:`music_scan.mb_release`, the canon's exact rungs, never
  a fuzzy match) into the ``mb-releases`` cache, :data:`LOOKUPS_PER_TICK` a
  tick.  The resolve pass then keys the row (requested next tick) or marks it
  ``no-match``.
- **otherwise** (looked up, no release group): the record is dropped, an
  ``album_dropped`` event saying why, as an album that leaves its last playlist
  is.  ``request_album`` brings it back by hand.

Every event is recorded with source :data:`SOURCE`, so the Music MCP's
``find_events`` with ``source=stranded`` lists what the sweep did.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Collection

import psycopg

from music_fetch import album_requests, events, later_queue
from music_fetch import state as statedb

logger = logging.getLogger(__name__)

# The requests' requested_by: a stranded row's automatic request.
BY = "auto:stranded"
# The events' source: find_events(source=stranded) lists the sweep's decisions.
SOURCE = "stranded"
# Requests a tick makes at most (each costs two or three MusicBrainz reads, one a second).
PER_TICK = 5
# Spotify albums looked up in MusicBrainz a tick at most (up to five reads each).
LOOKUPS_PER_TICK = 5
# The album statuses a row is stranded in: Usenet missed or failed, and the
# fallback that follows (albums.MISSING, FAILED, FALLBACK; albums imports this module).
STATUSES = ("missing", "failed", "fallback")
FALLBACK, MISSING, WANTED = "fallback", "missing", "wanted"

Row = tuple[str, dict]
# lookup(rows): look the rows' Spotify albums up in MusicBrainz into the mb-releases cache.
Lookup = Callable[[list[Row]], int]


def dead(record: dict, album_playlists: Collection[str]) -> bool:
    """An album playlist's row that no playlist in *album_playlists* lists and no
    request holds: nothing reads its tracks.  A feed's row (wants, library) has
    its own lifecycle, and a request's is :func:`albums.feed_requests`'s."""
    playlists = record.get("playlists") or {}
    return ((record.get("origin") or statedb.ALBUM) == statedb.ALBUM and not record.get("requested")
            and album_requests.SOURCE not in playlists and not any(n in album_playlists for n in playlists))


def _label(key: str, record: dict) -> str:
    return f"{key} {record.get('artist')} — {record.get('name')}"


def _gone(record: dict) -> str:
    return ", ".join(record.get("playlists") or {}) or "no playlist"


@dataclasses.dataclass
class Plan:
    """The stranded rows, by what the sweep does with each."""

    later: list[Row]    # on Later and in fallback: back to missing
    request: list[Row]  # a release group and no request: request it
    lookup: list[Row]   # no release group yet: look its Spotify album up
    drop: list[Row]     # looked up, no release group, not on Later: drop the record

    def counts(self) -> str:
        return (f"{len(self.later)} on Later back to missing, {len(self.request)} to request, "
                f"{len(self.lookup)} to look up in MusicBrainz, {len(self.drop)} to drop")


def plan(conn: psycopg.Connection, rows: statedb.AlbumRows, album_playlists: Collection[str]) -> Plan:
    """What :func:`run` would do, from the database alone."""
    requested = {g for (g,) in conn.execute("SELECT release_group::text FROM album_requests")}
    index = later_queue.Index(rows)
    on_later = {found[0] for found in map(index.find, later_queue.load(conn)) if found}
    out = Plan([], [], [], [])
    for key, record in rows.albums.items():
        if record.get("status") not in STATUSES or not dead(record, album_playlists):
            continue
        group = record.get("release_group")
        if key in on_later:
            if record.get("status") == FALLBACK:
                out.later.append((key, record))
            if not group and record.get("spotify_album_ids"):
                out.lookup.append((key, record))  # the lookup retries a miss weekly
        elif group:
            if group not in requested:
                out.request.append((key, record))
        elif record.get("unresolved") == statedb.PENDING and record.get("spotify_album_ids"):
            out.lookup.append((key, record))
        elif record.get("unresolved") != statedb.IN_FLIGHT:
            out.drop.append((key, record))
    return out


@dataclasses.dataclass
class Result:
    later: int = 0
    requested: int = 0
    dropped: int = 0
    looked_up: int = 0
    stopped: str = ""  # why the tick's requests stopped early (a MusicBrainz outage)


def _event(kind: str, key: str, record: dict, message: str, **data) -> None:
    events.record(kind, message, source=SOURCE, spotify_album_id=statedb.primary_spotify(record),
                  release_group=record.get("release_group"),
                  data={"album": f"{record.get('artist')} — {record.get('name')}", "key": key,
                        "origin": record.get("origin") or statedb.ALBUM, **data})


def _back_to_missing(key: str, record: dict) -> None:
    """A Later album's dead fallback ends: album mode searches it again, weekly once it has a search time."""
    playlists = _gone(record)
    record["status"] = MISSING if record.get("searched_at") else WANTED
    for field in ("fallback_from", "fallback_at", "fallback_tracks"):
        record.pop(field, None)
    message = (f"[STRANDED] {_label(key, record)}: its fallback was left to {playlists}, which left "
               f"playlists.conf; Later requests it once it has a release group, and Usenet is searched "
               f"meanwhile ({record['status']})")
    logger.info(message)
    _event("status_changed", key, record, message, why="its playlist left playlists.conf; on Later",
           playlists=list(record.get("playlists") or {}), **{"from": FALLBACK, "to": record["status"]})


def _drop(rows: statedb.AlbumRows, key: str, record: dict, why: str) -> None:
    message = (f"[STRANDED] {_label(key, record)}: no playlist in playlists.conf lists it (last: "
               f"{_gone(record)}) and it has no request; {why}; record dropped")
    logger.info(message)
    _event("album_dropped", key, record, message, status=record.get("status"),
           playlists=list(record.get("playlists") or {}), why=why)
    del rows.albums[key]


def _no_group(record: dict) -> str:
    """Why a stranded row has no release group to request it by."""
    if not record.get("spotify_album_ids"):
        return "it has no Spotify album to find its release group by"
    if record.get("unresolved") == statedb.CONFLICT:
        return "its evidence names two release groups (conflict), so it can't be requested automatically"
    return "MusicBrainz knows no release group for it (looked up by URL, ISRC and barcode)"


def run(conn: psycopg.Connection, rows: statedb.AlbumRows, album_playlists: Collection[str],
        get: album_requests.Get | None, lookup: Lookup | None) -> tuple[Plan, Result]:
    """Settle :func:`plan`'s rows in *rows* (the tick saves them): Later's back to
    missing, the first :data:`PER_TICK` requests (with *get*), the lookups (with
    *lookup*), the drops."""
    p = plan(conn, rows, album_playlists)
    result = Result()
    for key, record in p.later:
        _back_to_missing(key, record)
        result.later += 1
    for key, record in p.drop:
        _drop(rows, key, record, _no_group(record))
        result.dropped += 1
    for key, record in (p.request[:PER_TICK] if get is not None else []):
        group = record["release_group"]
        try:
            tracklist = album_requests.tracklist(get, group)
        except album_requests.Refused as exc:
            _drop(rows, key, record, f"MusicBrainz gives no tracklist for release group {group} ({exc})")
            result.dropped += 1
            continue
        except OSError as exc:  # urllib's errors: MusicBrainz down or refusing
            result.stopped = f"reading release group {group} from MusicBrainz failed: {exc}"
            break
        if album_requests.add(conn, tracklist, BY, key, record, source=SOURCE,
                              note=f"; automatic ({BY}): {_gone(record)} left playlists.conf"):
            result.requested += 1
            logger.info("[STRANDED] %s: requested automatically, %d track(s) from release %s",
                        _label(key, record), len(tracklist.tracks), tracklist.release)
    if p.lookup and lookup is not None:
        result.looked_up = lookup(p.lookup)
    return p, result


def tick(rows: statedb.AlbumRows, album_playlists: Collection[str], get: album_requests.Get | None,
         lookup: Lookup | None, apply: bool) -> None:
    """The album tick's step: :func:`run` when *apply* (album mode ``on``), else
    only :func:`plan`'s counts.  One ``[STRANDED]`` count line.  Never raises: a
    failure here mustn't stop the tick, and the next one tries again."""
    try:
        with statedb.session() as conn:
            if not apply:
                logger.info("[STRANDED] dry run: %s", plan(conn, rows, album_playlists).counts())
                return
            p, result = run(conn, rows, album_playlists, get, lookup)
    except Exception:
        logger.exception("[STRANDED] settling the stranded album rows failed; the next tick tries again")
        return
    if p.later or p.request or p.lookup or p.drop:
        logger.info("[STRANDED] %d on Later back to missing, %d requested, %d dropped, %d looked up in "
                    "MusicBrainz (of %d waiting for a release group)%s", result.later, result.requested,
                    result.dropped, result.looked_up, len(p.lookup),
                    f"; stopped: {result.stopped}" if result.stopped else "")


class _MusicBrainz:
    """A MusicBrainz get as :class:`music_scan.mb_release.Resolver` takes it."""

    def __init__(self, get: album_requests.Get) -> None:
        self.get = get


def musicbrainz_lookup(get: album_requests.Get, budget: int = LOOKUPS_PER_TICK,
                       upc_of: Callable[[str], str | None] | None = None) -> Lookup:
    """A :data:`Lookup` through the canon's resolver and its ``mb-releases``
    cache (:class:`music_scan.mb_release.Resolver`), sharing *get*'s rate limit;
    at most *budget* Spotify albums are read a call, and a miss is read again
    after the resolver's retry (7 days).  *upc_of*: the barcode rung's Spotify
    read (default: the resolver's own)."""

    def lookup(rows: list[Row]) -> int:
        from music_scan.mb_release import Resolver, SpotifyAlbum  # noqa: PLC0415 — the image holds both packages

        extra = {} if upc_of is None else {"upc_of": upc_of}
        resolver = Resolver(budget=budget, mb=_MusicBrainz(get), **extra)  # type: ignore[arg-type]
        for _, record in rows:
            ids = list(record.get("spotify_album_ids") or [])
            isrcs = tuple(dict.fromkeys(t[3] for tracks in (record.get("playlists") or {}).values()
                                        for t in tracks if len(t) > 3 and t[3]))
            for album_id in ids:
                resolver.fields(SpotifyAlbum(album_id, record.get("name") or "", int(record.get("tracks_count") or 0),
                                             isrcs, tuple(i for i in ids if i != album_id)))
        resolver.save()
        return resolver.looked_up

    return lookup

"""Automatic requests for Later's incomplete albums (homelab#2535).

Since the stage 3 cutover (homelab#2478) ``later`` is no longer an album
playlist, so spotdl no longer gets its albums' Spotify fallback tracks, and
the MusicBrainz-built fallback covers requests only.  The operator chose on
2026-10-31 to request them automatically: each album tick
(:func:`albums.tick`, before :func:`albums.feed_requests`) requests every
Later album that

- has a release group (an entry queued by ``ref`` gets one once
  :func:`later_queue.resolve_refs` gives it its row's, at a scan's regen),
- isn't held whole (its row isn't ``have``, ``imported`` or ``filled``), and
- has no request yet (``album_requests`` and ``later_queue`` are both unique on
  release group, so an album is requested once, ever).

The request is an ordinary one (:func:`album_requests.add`), with
``requested_by`` :data:`BY`, so ``list_requests`` and its
``album_requested`` event tell it from a hand request.  From then on it is
searched on Usenet and falls back like any request; a partial fallback stays
``fallback`` and is searched again weekly, and is never requested again.
Leaving Later doesn't withdraw it.

Each request reads its tracklist from MusicBrainz (two or three reads, one a
second), so a tick makes at most :data:`PER_TICK`; the rest wait for the next
tick.  A release group MusicBrainz can't give a tracklist for (no official
release with tracks) is skipped for :data:`RETRY`, recorded as a ``failure``
event with ``data.what`` :data:`WHAT` (the digest lists it); a MusicBrainz
outage stops the tick's requests, and the next tick tries again.

``music-later auto-requests`` (:mod:`music_fetch.later_cli`) prints
:func:`plan`'s lines without writing or reading MusicBrainz: what the next
tick would request.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

import psycopg

from music_fetch import album_requests, events, later_queue
from music_fetch import state as statedb

logger = logging.getLogger(__name__)

# The requests' requested_by: an automatic request for a Later album.
BY = "auto:later"
# Requests a tick makes at most (each costs two or three MusicBrainz reads, one a second).
PER_TICK = 5
# How long a release group MusicBrainz refused a tracklist for is skipped.
RETRY = timedelta(days=7)
# The data.what of a refused request's failure event.
WHAT = "later_request"

Row = tuple[str, dict]


@dataclasses.dataclass
class Plan:
    """Later's albums that aren't held whole and have no request, in queue order."""

    request: list[tuple[later_queue.Entry, Row | None]]  # to request, with the row that has the release group
    unresolved: list[tuple[later_queue.Entry, Row | None]]  # queued by ref: no release group yet
    refused: list[tuple[later_queue.Entry, Row | None]]  # refused in the last RETRY: skipped

    def counts(self) -> str:
        return (f"{len(self.request)} to request, {len(self.unresolved)} waiting for a release group, "
                f"{len(self.refused)} refused by MusicBrainz in the last {RETRY.days} days")


def refused_groups(conn: psycopg.Connection) -> set[str]:
    """The release groups whose automatic request MusicBrainz refused in the last :data:`RETRY`."""
    return {g for (g,) in conn.execute(
        "SELECT DISTINCT release_group::text FROM events WHERE kind = 'failure' AND data->>'what' = %s "
        "AND release_group IS NOT NULL AND at > now() - %s", (WHAT, RETRY))}


def plan(conn: psycopg.Connection, rows: statedb.AlbumRows) -> Plan:
    """What the next tick would request (:func:`run`), from the database alone."""
    requested = {r for (r,) in conn.execute("SELECT release_group::text FROM album_requests")}
    refused = refused_groups(conn)
    index = later_queue.Index(rows)
    out = Plan([], [], [])
    for entry in later_queue.load(conn):
        found = index.find(entry)
        if later_queue.held(found[1] if found else None) or entry.release_group in requested:
            continue
        if not entry.release_group:
            out.unresolved.append((entry, found))
        elif entry.release_group in refused:
            out.refused.append((entry, found))
        else:
            out.request.append((entry, found))
    return out


def _label(entry: later_queue.Entry, found: Row | None) -> str:
    return later_queue.label(entry.release_group, found, {}, entry.ref)


def _named(entry: later_queue.Entry, found: Row | None) -> str:
    """``artist — name [release group …]`` (or ``[album ref …]``), or the bare
    release group when no row names it."""
    label = _label(entry, found)
    if label.startswith(("release group ", "album ref ")):
        return label
    return f"{label} [release group {entry.release_group}]" if entry.release_group else f"{label} [album ref {entry.ref}]"


def lines(p: Plan) -> list[str]:
    """``music-later auto-requests``'s lines: each album, then the counts."""
    out = []
    for n, (entry, found) in enumerate(p.request, 1):
        status = found[1].get("status") if found else "no album row yet"
        out.append(f"Request {n} of {len(p.request)}: {_named(entry, found)}: {status}"
                   + ("; this tick" if n <= PER_TICK else "; a later tick"))
    for entry, found in p.unresolved:
        status = found[1].get("status") if found else "no album row"
        out.append(f"Waiting: {_named(entry, found)}: {status}; requested once its row has a release group")
    for entry, found in p.refused:
        out.append(f"Refused: {_named(entry, found)}: MusicBrainz gave no tracklist; tried again "
                   f"{RETRY.days} days after")
    out.append(f"Later: {p.counts()}; a tick requests {PER_TICK} at most")
    return out


@dataclasses.dataclass
class Result:
    requested: int = 0
    refused: int = 0
    stopped: str = ""  # why the tick's requests stopped early (a MusicBrainz outage)


def run(conn: psycopg.Connection, rows: statedb.AlbumRows, get: album_requests.Get,
        limit: int | None = None) -> tuple[Plan, Result]:
    """Request the first *limit* (:data:`PER_TICK`) albums of :func:`plan`,
    each in its own transaction with its ``album_requested`` event; record a
    refused one's ``failure`` event instead."""
    p = plan(conn, rows)
    result = Result()
    for entry, found in p.request[:PER_TICK if limit is None else limit]:
        group = entry.release_group
        if group is None:  # plan() puts only entries with a release group in request
            continue
        key, record = found if found else (None, None)
        named = _named(entry, found)
        try:
            tracklist = album_requests.tracklist(get, group)
        except album_requests.Refused as exc:
            result.refused += 1
            with conn.transaction():
                events.insert(conn, "failure",
                              f"[FAIL] Later: {named}: no automatic request: {exc}; tried again in "
                              f"{RETRY.days} days", source="later", release_group=group,
                              spotify_album_id=statedb.primary_spotify(record) if record else None,
                              data={"what": WHAT, "album": _label(entry, found), "key": key, "reason": str(exc)})
            logger.warning("[LATER] %s: no automatic request: %s", named, exc)
            continue
        except OSError as exc:  # urllib's errors: MusicBrainz down or refusing
            result.stopped = f"reading release group {group} from MusicBrainz failed: {exc}"
            break
        if album_requests.add(conn, tracklist, BY, key, record, note=f"; automatic ({BY}): incomplete on Later"):
            result.requested += 1
            logger.info("[LATER] %s: requested automatically, %d track(s) from release %s", named,
                        len(tracklist.tracks), tracklist.release)
    return p, result


def tick(rows: statedb.AlbumRows, get: album_requests.Get | None, apply: bool) -> None:
    """The album tick's step: :func:`run` when *apply* (album mode ``on``),
    else only :func:`plan`'s counts.  One ``[LATER-REQ]`` line.  Never raises:
    a failure here mustn't stop the tick, and the next one tries again."""
    try:
        with statedb.session() as conn:
            if conn is None:
                return  # the JSON files are the state: no Later queue
            if not apply or get is None:
                logger.info("[LATER-REQ] dry run: %s", plan(conn, rows).counts())
                return
            p, result = run(conn, rows, get)
    except Exception:
        logger.exception("[LATER-REQ] automatic requests for Later failed; the next tick tries again")
        return
    left = len(p.request) - result.requested - result.refused
    logger.info("[LATER-REQ] %d requested automatically, %d refused by MusicBrainz, %d left for the next tick; "
                "%d waiting for a release group, %d refused earlier%s", result.requested, result.refused, left,
                len(p.unresolved), len(p.refused), f"; stopped: {result.stopped}" if result.stopped else "")

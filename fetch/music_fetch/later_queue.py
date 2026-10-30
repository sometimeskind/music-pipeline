"""The Later queue (homelab#2478 stage 2): albums to listen to, in arrival order.

A ``later_queue`` row (migration 8 in :mod:`music_fetch.db`) names an album by
MusicBrainz release group.  Albums are appended by a request with ``later``
(``music-requests add --later``, the Music MCP's ``request_album``), and leave
only by explicit removal (:func:`remove`, ``music-later remove``): by release
group, or every album up to a position (``--through``).  Leaving touches no
files, and there is no removal from play counts.

``available_at`` is when the library first held the album whole: set when it
is added if it is held already, else by the first ``.m3u`` regen that finds it
held (:func:`mark_available`; a scan, and so a regen, follows every import).
A reset or reimport doesn't move it.  The Later ``.m3u`` (:data:`PLAYLIST`,
written by ``music_scan.scan.regen_playlists``) lists the available albums in
``available_at`` order, so an album that arrives late joins the end, not
behind the listening position.  Pending albums aren't in it.

Until the stage 3 cutover the Spotify album playlist ``later`` still has its
store: the queue's albums then follow that playlist's albums in the same
``later.m3u``, each file once.  Once ``later`` leaves ``playlists.conf`` the
queue alone writes it.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime

import psycopg

from music_fetch import album_requests, events
from music_fetch import state as statedb

logger = logging.getLogger(__name__)

# The .m3u the queue writes: the album playlist's name, so the cutover keeps the file.
PLAYLIST = "later"
# An album row in one of these is held whole (albums.HAVE, IMPORTED, FILLED).
HELD = ("have", "imported", "filled")
# An entry's state: in the .m3u, or waiting for the library to hold its album.
AVAILABLE, PENDING = "available", "pending"


@dataclasses.dataclass
class Entry:
    id: int
    release_group: str
    added_at: datetime
    available_at: datetime | None = None


def load(conn: psycopg.Connection) -> list[Entry]:
    """Every album on Later: the available ones in ``.m3u`` order, then the
    pending ones in the order they were added."""
    return [Entry(*row) for row in conn.execute(
        "SELECT id, release_group::text, added_at, available_at FROM later_queue "
        "ORDER BY available_at NULLS LAST, added_at, id")]


def held(record: Mapping | None) -> bool:
    return record is not None and record.get("status") in HELD


def by_group(rows: statedb.AlbumRows) -> dict[str, tuple[str, dict]]:
    """Each album row's release group → ``(key, record)``: unique, as on the table."""
    return {r["release_group"]: (k, r) for k, r in rows.albums.items() if r.get("release_group")}


def names(group: str, found: tuple[str, dict] | None,
          requests: Mapping[str, album_requests.Request]) -> tuple[str, str]:
    """``(artist, name)`` of release group *group*: its album row's (*found*), else its request's."""
    if found is not None and found[1].get("name"):
        return found[1].get("artist", ""), found[1]["name"]
    if group in requests:
        return requests[group].artist, requests[group].name
    return "", ""


def label(group: str, found: tuple[str, dict] | None, requests: Mapping[str, album_requests.Request]) -> str:
    artist, name = names(group, found, requests)
    return f"{artist} — {name}" if name else f"release group {group}"


def requests_by_group(conn: psycopg.Connection) -> dict[str, album_requests.Request]:
    return {r.release_group: r for r in album_requests.load(conn)}


def add(conn: psycopg.Connection, groups: Iterable[str], rows: statedb.AlbumRows,
        requests: Mapping[str, album_requests.Request]) -> int:
    """Append release groups *groups* to Later, in order, each with its
    ``later_added`` event, in the caller's transaction; one queued already is
    left where it is.  One held whole already is available at once.  Returns
    how many were added."""
    added = 0
    index = by_group(rows)
    for group in groups:
        found = index.get(group)
        key, record = found if found else (None, None)
        now = held(record)
        row = conn.execute(
            "INSERT INTO later_queue (release_group, available_at) VALUES (%s, CASE WHEN %s THEN now() END) "
            "ON CONFLICT (release_group) DO NOTHING RETURNING id", (group, now)).fetchone()
        if row is None:
            continue
        added += 1
        album = label(group, found, requests)
        events.insert(conn, "later_added",
                      f"[LATER] {album} ({group}): added" + (", available now" if now else ", pending its import"),
                      source="later", release_group=group,
                      spotify_album_id=statedb.primary_spotify(record) if record else None,
                      data={"album": album, "key": key, "available": now})
    return added


def remove(conn: psycopg.Connection, entries: Iterable[Entry], rows: statedb.AlbumRows,
           requests: Mapping[str, album_requests.Request]) -> int:
    """Take *entries* off Later, each with its ``later_removed`` event, in the
    caller's transaction.  Their files stay in the library.  Returns how many
    were removed (one removed meanwhile is not counted)."""
    removed = 0
    index = by_group(rows)
    for entry in entries:
        if conn.execute("DELETE FROM later_queue WHERE release_group = %s",
                        (entry.release_group,)).rowcount == 0:
            continue
        removed += 1
        found = index.get(entry.release_group)
        key, record = found if found else (None, None)
        album = label(entry.release_group, found, requests)
        events.insert(conn, "later_removed", f"[LATER] {album} ({entry.release_group}): removed",
                      source="later", release_group=entry.release_group,
                      spotify_album_id=statedb.primary_spotify(record) if record else None,
                      data={"album": album, "key": key, "added_at": album_requests.iso(entry.added_at),
                            "available_at": album_requests.iso(entry.available_at)})
    return removed


def mark_available(conn: psycopg.Connection, rows: statedb.AlbumRows) -> list[str]:
    """Set ``available_at`` on every pending album the library now holds
    whole, and return their release groups: they join the end of the
    ``.m3u``.  Logs one ``[LATER]`` line when any did."""
    index = by_group(rows)
    ready = [e.release_group for e in load(conn)
             if e.available_at is None and held((index.get(e.release_group) or (None, None))[1])]
    if not ready:
        return []
    done = [g for (g,) in conn.execute(
        "UPDATE later_queue SET available_at = now() WHERE release_group = ANY(%s::uuid[]) AND available_at IS NULL "
        "RETURNING release_group::text", (ready,))]
    if done:
        logger.info("[LATER] %d album(s) now held whole, at the end of %s.m3u", len(done), PLAYLIST)
    return done


def albums(conn: psycopg.Connection, rows: statedb.AlbumRows) -> list[tuple[str, dict]]:
    """The available albums' rows ``(key, record)``, in ``.m3u`` order.  An
    album whose row is gone (its release group on no row) is left out."""
    out = []
    index = by_group(rows)
    for entry in load(conn):
        if entry.available_at is None:
            break
        found = index.get(entry.release_group)
        if found is None:
            logger.warning("[LATER] release group %s: no album row has it; left out of %s.m3u",
                           entry.release_group, PLAYLIST)
            continue
        out.append(found)
    return out


def listing(conn: psycopg.Connection, rows: statedb.AlbumRows) -> list[dict]:
    """Every album on Later as ``get_later`` shows it, in :func:`load`'s order:
    ``position`` (1-based, the available ones only), its album row's key,
    status and origin (``status`` ``pending`` while the album tick hasn't
    queued its request), and when it was added and became available."""
    requests = requests_by_group(conn)
    index = by_group(rows)
    out = []
    position = 0
    for entry in load(conn):
        found = index.get(entry.release_group)
        album = None
        if found is not None:
            key, record = found
            album = {"key": key, "status": record.get("status"), "origin": record.get("origin") or statedb.ALBUM}
        if entry.available_at is not None:
            position += 1
        artist, name = names(entry.release_group, found, requests)
        out.append({"position": position if entry.available_at is not None else None,
                    "state": AVAILABLE if entry.available_at is not None else PENDING,
                    "release_group": entry.release_group, "artist": artist, "name": name,
                    "status": album["status"] if album else album_requests.PENDING, "album": album,
                    "requested": entry.release_group in requests,
                    "added_at": album_requests.iso(entry.added_at),
                    "available_at": album_requests.iso(entry.available_at)})
    return out

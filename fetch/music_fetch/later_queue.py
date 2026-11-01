"""The Later queue (homelab#2478 stage 2): albums to listen to, in arrival order.

A ``later_queue`` row (migration 8 in :mod:`music_fetch.db`) names an album by
MusicBrainz release group.  Albums are appended by a request with ``later``
(``music-requests add --later``, the Music MCP's ``request_album``), and leave
only by explicit removal (:func:`remove`, ``music-later remove``): by release
group, by album (``--album``, homelab#2534), or every album up to a position
(``--through``).  Leaving touches no
files, and there is no removal from play counts.

An album the stage 3 cutover (``music-cutover``) queued from the later
playlist before its row had a release group names it by ``ref`` instead
(migration 9): one of its album refs, its primary Spotify album ID, which
follows the album through a merge.  The regen gives such an entry its row's
release group once the row has one (:func:`resolve_refs`); until then it is
removed by album or by ``--through``, not by release group.

``available_at`` is when the library first held the album whole: set when it
is added if it is held already, else by the first ``.m3u`` regen that finds it
held (:func:`mark_available`; a scan, and so a regen, follows every import).
A reset or reimport doesn't move it.  The Later ``.m3u`` (:data:`PLAYLIST`,
written by ``music_scan.scan.regen_playlists``) lists the available albums in
``available_at`` order, so an album that arrives late joins the end, not
behind the listening position.  Pending albums aren't in it.

The queue alone writes ``later.m3u``: the Spotify album playlist ``later``
left ``playlists.conf`` at the stage 3 cutover, and a store by that name is
not read for it.
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
    release_group: str | None
    added_at: datetime
    available_at: datetime | None = None
    ref: str | None = None  # the album ref an entry with no release group yet names its album by


def load(conn: psycopg.Connection) -> list[Entry]:
    """Every album on Later: the available ones in ``.m3u`` order, then the
    pending ones in the order they were added."""
    return [Entry(*row) for row in conn.execute(
        "SELECT id, release_group::text, added_at, available_at, ref FROM later_queue "
        "ORDER BY available_at NULLS LAST, added_at, id")]


def held(record: Mapping | None) -> bool:
    return record is not None and record.get("status") in HELD


def by_group(rows: statedb.AlbumRows) -> dict[str, tuple[str, dict]]:
    """Each album row's release group → ``(key, record)``: unique, as on the table."""
    return {r["release_group"]: (k, r) for k, r in rows.albums.items() if r.get("release_group")}


class Index:
    """The album rows an entry names: by release group, else by its ref."""

    def __init__(self, rows: statedb.AlbumRows) -> None:
        self.rows = rows
        self.groups = by_group(rows)
        self.refs = rows.by_ref()

    def find(self, entry: Entry) -> tuple[str, dict] | None:
        if entry.release_group:
            return self.groups.get(entry.release_group)
        key = self.refs.get(entry.ref or "")
        return None if key is None else (key, self.rows.albums[key])


def ref(record: Mapping) -> str | None:
    """The ref an album row with no release group is queued by: its primary
    Spotify album ID, else its local name."""
    return statedb.primary_spotify(record) or next(iter(record.get("names") or ()), None)


def names(group: str | None, found: tuple[str, dict] | None,
          requests: Mapping[str, album_requests.Request]) -> tuple[str, str]:
    """``(artist, name)`` of release group *group*: its album row's (*found*), else its request's."""
    if found is not None and found[1].get("name"):
        return found[1].get("artist", ""), found[1]["name"]
    if group in requests:
        return requests[group].artist, requests[group].name
    return "", ""


def label(group: str | None, found: tuple[str, dict] | None, requests: Mapping[str, album_requests.Request],
          entry_ref: str | None = None) -> str:
    artist, name = names(group, found, requests)
    return f"{artist} — {name}" if name else (f"release group {group}" if group else f"album ref {entry_ref}")


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
    index = Index(rows)
    for entry in entries:
        if conn.execute("DELETE FROM later_queue WHERE id = %s", (entry.id,)).rowcount == 0:
            continue
        removed += 1
        found = index.find(entry)
        key, record = found if found else (None, None)
        album = label(entry.release_group, found, requests, entry.ref)
        events.insert(conn, "later_removed", f"[LATER] {album} ({entry.release_group or entry.ref}): removed",
                      source="later", release_group=entry.release_group,
                      spotify_album_id=statedb.primary_spotify(record) if record else None,
                      data={"album": album, "key": key, "ref": entry.ref,
                            "added_at": album_requests.iso(entry.added_at),
                            "available_at": album_requests.iso(entry.available_at)})
    return removed


def resolve_refs(conn: psycopg.Connection, rows: statedb.AlbumRows) -> int:
    """Give every entry named by ref alone its album row's release group once
    the row has one, and keep the ref.  When an entry has that release group
    already (the album was requested with ``later`` meanwhile), the two are one
    album: the first in queue order stays, with the earlier times of the two.
    Returns how many entries were resolved.  Logs one ``[LATER]`` line each."""
    index = Index(rows)
    entries = load(conn)
    on = {e.release_group: e for e in entries if e.release_group}
    done = 0
    for entry in entries:
        if entry.release_group or (found := index.find(entry)) is None or not found[1].get("release_group"):
            continue
        group = found[1]["release_group"]
        album = label(group, found, {}, entry.ref)
        other = on.get(group)
        if other is None:
            conn.execute("UPDATE later_queue SET release_group = %s WHERE id = %s", (group, entry.id))
            on[group] = entry
            logger.info("[LATER] %s (%s): resolved to release group %s", album, entry.ref, group)
        else:
            first, second = (entry, other) if entries.index(entry) < entries.index(other) else (other, entry)
            conn.execute("DELETE FROM later_queue WHERE id = %s", (second.id,))
            conn.execute(
                "UPDATE later_queue SET release_group = %s, ref = coalesce(ref, %s), added_at = least(added_at, %s), "
                "available_at = CASE WHEN available_at IS NULL THEN %s ELSE least(available_at, coalesce(%s, available_at)) END "
                "WHERE id = %s",
                (group, second.ref, second.added_at, second.available_at, second.available_at, first.id))
            on[group] = first
            logger.info("[LATER] %s (%s): resolved to release group %s, on Later already; one entry kept",
                        album, entry.ref, group)
        done += 1
    return done


def mark_available(conn: psycopg.Connection, rows: statedb.AlbumRows) -> list[int]:
    """Set ``available_at`` on every pending album the library now holds
    whole, and return their entries' ids: they join the end of the ``.m3u``.
    Logs one ``[LATER]`` line when any did."""
    index = Index(rows)
    ready = [e.id for e in load(conn) if e.available_at is None and held((index.find(e) or (None, None))[1])]
    if not ready:
        return []
    done = [i for (i,) in conn.execute(
        "UPDATE later_queue SET available_at = now() WHERE id = ANY(%s) AND available_at IS NULL RETURNING id",
        (ready,))]
    if done:
        logger.info("[LATER] %d album(s) now held whole, at the end of %s.m3u", len(done), PLAYLIST)
    return done


def albums(conn: psycopg.Connection, rows: statedb.AlbumRows) -> list[tuple[str, dict]]:
    """The available albums' rows ``(key, record)``, in ``.m3u`` order.  An
    album whose row is gone (no row has its release group or ref) is left out."""
    out = []
    index = Index(rows)
    for entry in load(conn):
        if entry.available_at is None:
            break
        found = index.find(entry)
        if found is None:
            logger.warning("[LATER] %s %s: no album row has it; left out of %s.m3u",
                           "release group" if entry.release_group else "album ref",
                           entry.release_group or entry.ref, PLAYLIST)
            continue
        out.append(found)
    return out


def listing(conn: psycopg.Connection, rows: statedb.AlbumRows) -> list[dict]:
    """Every album on Later as ``get_later`` shows it, in :func:`load`'s order:
    ``position`` (1-based, the available ones only), its album row's key,
    status and origin (``status`` ``pending`` while the album tick hasn't
    queued its request), its ``ref`` (an album queued before its row had a
    release group, which is then null), and when it was added and became
    available."""
    requests = requests_by_group(conn)
    index = Index(rows)
    out = []
    position = 0
    for entry in load(conn):
        found = index.find(entry)
        album = None
        if found is not None:
            key, record = found
            album = {"key": key, "status": record.get("status"), "origin": record.get("origin") or statedb.ALBUM}
        if entry.available_at is not None:
            position += 1
        artist, name = names(entry.release_group, found, requests)
        out.append({"position": position if entry.available_at is not None else None,
                    "state": AVAILABLE if entry.available_at is not None else PENDING,
                    "release_group": entry.release_group, "ref": entry.ref, "artist": artist, "name": name,
                    "status": album["status"] if album else album_requests.PENDING, "album": album,
                    "requested": entry.release_group in requests,
                    "added_at": album_requests.iso(entry.added_at),
                    "available_at": album_requests.iso(entry.available_at)})
    return out

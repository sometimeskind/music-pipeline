"""``music-cutover``: move Later and Keep off Spotify, once (homelab#2478 stage 3).

A dry run unless ``--apply``.  It reads the album playlists' stores, so it runs
while ``later`` and ``keep`` are still in ``playlists.conf``; the lines are
dropped after its apply.

- **Later:** every album on the ``later`` store goes on the Later queue
  (:mod:`music_fetch.later_queue`) in the store's order, which is the order
  ``later.m3u`` lists them in.  Each gets an ``added_at``, and an
  ``available_at`` when the library holds it whole, a second apart and earlier
  than every entry the queue has, so the albums keep their place ahead of the
  ones requested since.  An album on the queue already moves to its store
  place.  Once ``later`` has no store the queue alone writes ``later.m3u``,
  and it lists what the store did.
- **Keep:** every album on the ``keep`` store becomes an album request
  (:mod:`music_fetch.album_requests`), its tracklist read from MusicBrainz
  by release group at ``--apply``; one requested already is left as it is,
  and one MusicBrainz can't give a tracklist for is listed and skipped (run
  ``--apply`` again for it).  Keep has no store or ``.m3u`` afterwards.
- **No release group:** the queue and the requests are keyed by release
  group, so an album whose row has none yet is listed and left out.

The dry run also renders ``later.m3u`` as the queue will write it and
compares it with the file the last scan wrote.  ``--apply`` writes the queue
entries, the requests and the :data:`state.CUTOVER` marker in one
transaction, under the ``albums`` and ``pipeline`` locks; run again, it adds
what is missing.  ``--revert --apply`` takes off what the marker lists:
the queue entries it added (``later_removed``), each moved entry back to
its place before, and the requests it added.  A row the album tick has
marked ``requested`` keeps the marker, which only keeps the row when it
leaves its playlists.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

import psycopg

from music_fetch import album_requests as req
from music_fetch import events, later_queue
from music_fetch import state as statedb
from music_fetch import store

logger = logging.getLogger(__name__)

LATER, KEEP = later_queue.PLAYLIST, "keep"
# Between two imported Later albums' times: only their order matters.
SPACING = timedelta(seconds=1)
# Lines of a later.m3u difference listed.
DIFF_LISTED = 10

Row = tuple[str, dict]


def label(key: str, record: Mapping) -> str:
    return f"{record.get('artist', '')} — {record.get('name', '')} (album {key})"


@dataclasses.dataclass
class Plan:
    later: list[Row]                  # to queue, in the store's order
    keep: list[Row]                   # to request
    requested: list[Row]              # keep albums requested already
    unresolved: dict[str, list[Row]]  # playlist → its albums whose row has no release group
    unknown: dict[str, list[str]]     # playlist → its albums no row holds


def store_albums(rows: statedb.AlbumRows, songs: list[dict]) -> tuple[list[Row], list[str]]:
    """The album rows a store's *songs* are on, each once, in the order the
    store first lists them (the ``.m3u``'s), and the labels of the store's
    albums no row holds."""
    from music_scan.playlists import album_groups  # noqa: PLC0415

    refs = rows.by_ref()
    out: dict[str, dict] = {}
    unknown: list[str] = []
    for album_id, group in album_groups(songs).items():
        key = refs.get(album_id)
        if key is None:
            unknown.append(f"{group[0].get('album_artist', '')} — {group[0].get('album_name', '')} ({album_id})")
        else:
            out.setdefault(key, rows.albums[key])
    return list(out.items()), unknown


def plan(rows: statedb.AlbumRows, later_songs: list[dict], keep_songs: list[dict],
         requested: Iterable[str]) -> Plan:
    """What the cutover does with the ``later`` and ``keep`` stores' albums;
    *requested*: the release groups requested already."""
    requested = set(requested)
    out = Plan([], [], [], {LATER: [], KEEP: []}, {})
    later_rows, out.unknown[LATER] = store_albums(rows, later_songs)
    keep_rows, out.unknown[KEEP] = store_albums(rows, keep_songs)
    for key, record in later_rows:
        (out.later if record.get("release_group") else out.unresolved[LATER]).append((key, record))
    for key, record in keep_rows:
        group = record.get("release_group")
        if not group:
            out.unresolved[KEEP].append((key, record))
        else:
            (out.requested if group in requested else out.keep).append((key, record))
    return out


# ---------------------------------------------------------------------------
# Later
# ---------------------------------------------------------------------------


def queue_times(conn: psycopg.Connection, n: int) -> list[datetime]:
    """*n* times a :data:`SPACING` apart, in order, the last one before every
    time the queue has (and before now)."""
    (first,) = conn.execute(
        "SELECT least(now(), min(least(added_at, coalesce(available_at, added_at)))) FROM later_queue").fetchone()
    return [first - SPACING * (n - i) for i in range(n)]


def queue_after(entries: list[later_queue.Entry], rows: statedb.AlbumRows, imported: list[Row]) -> list[Row]:
    """The available albums of the queue after the cutover, ``(key, record)``
    in ``.m3u`` order: the *imported* Later albums the library holds whole,
    then the queue's other available ones in their order."""
    groups = {r["release_group"] for _, r in imported}
    index = later_queue.by_group(rows)
    rest = [index[e.release_group] for e in entries
            if e.available_at is not None and e.release_group not in groups and e.release_group in index]
    return [(k, r) for k, r in imported if later_queue.held(r)] + rest


def write_queue(conn: psycopg.Connection, imported: list[Row],
                previous: Mapping[str, dict | None]) -> dict[str, dict | None]:
    """Put the *imported* Later albums on the queue in order, each with its
    ``later_added`` event, in the caller's transaction.  Returns each one's
    entry before (None: it had none) for the marker; *previous* is the
    marker's from an earlier apply, which wins."""
    before = {e.release_group: e for e in later_queue.load(conn)}
    out: dict[str, dict | None] = {}
    for n, ((key, record), at) in enumerate(zip(imported, queue_times(conn, len(imported))), 1):
        group = record["release_group"]
        old = before.get(group)
        out[group] = previous[group] if group in previous else (None if old is None else {
            "added_at": old.added_at.isoformat(),
            "available_at": old.available_at.isoformat() if old.available_at else None})
        available = later_queue.held(record)
        conn.execute(
            "INSERT INTO later_queue (release_group, added_at, available_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (release_group) DO UPDATE SET added_at = EXCLUDED.added_at, "
            "available_at = EXCLUDED.available_at", (group, at, at if available else None))
        album = f"{record.get('artist', '')} — {record.get('name', '')}"
        events.insert(conn, "later_added",
                      f"[LATER] {album} ({group}): from the later playlist, its album {n}"
                      + ("" if available else ", pending its import") + (", moved" if old else ""),
                      source="cutover", release_group=group, spotify_album_id=statedb.primary_spotify(record),
                      data={"album": album, "key": key, "available": available, "cutover": True,
                            "moved": old is not None})
    return out


# ---------------------------------------------------------------------------
# Keep
# ---------------------------------------------------------------------------


def tracklists(get: req.Get, keep: list[Row]) -> tuple[list[tuple[req.Tracklist, str, dict]], list[str]]:
    """Each Keep album's request tracklist from MusicBrainz, and the lines of
    the ones it can't give one for (skipped)."""
    found: list[tuple[req.Tracklist, str, dict]] = []
    skipped: list[str] = []
    for key, record in keep:
        try:
            found.append((req.tracklist(get, record["release_group"]), key, record))
        except (req.Refused, OSError) as exc:  # OSError: urllib's, MusicBrainz down or refusing
            skipped.append(f"  [SKIP] keep: {label(key, record)}: {exc}")
    return found, skipped


# ---------------------------------------------------------------------------
# The check, and the dry run's lines
# ---------------------------------------------------------------------------


def m3u_lines(found: list[Row]) -> list[str]:
    """``later.m3u``'s lines as the queue writes them for *found*."""
    from music_scan import playlists, scan  # noqa: PLC0415
    from music_scan.identity import ItemIndex  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415

    with MusicLibrary(scan.LIBRARY_DB) as lib:
        items = lib.all_items()
    paths = scan.later_paths(found, ItemIndex(items), playlists.by_album_id(items), playlists.by_stamp(items))
    return [os.path.relpath(p, scan.PLAYLISTS) for p in paths]


def current_m3u() -> list[str] | None:
    from music_scan.scan import PLAYLISTS  # noqa: PLC0415

    path = PLAYLISTS / f"{LATER}.m3u"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else None


def compare(now: list[str] | None, after: list[str]) -> list[str]:
    """The check's lines: ``later.m3u`` as the last scan wrote it (*now*)
    against the queue's (*after*)."""
    if now is None:
        return [f"later.m3u: none yet; the queue writes {len(after)} line(s)"]
    if now == after:
        return [f"later.m3u: unchanged by the cutover ({len(now)} line(s))"]
    gone = [line for line in now if line not in set(after)]
    new = [line for line in after if line not in set(now)]
    out = [f"later.m3u: CHANGED by the cutover: {len(now)} line(s) now, {len(after)} after; "
           f"{len(gone)} only now, {len(new)} only after"
           + ("; the same lines in another order" if not gone and not new else "")]
    out += [f"  - {line}" for line in gone[:DIFF_LISTED]] + [f"  + {line}" for line in new[:DIFF_LISTED]]
    if len(gone) > DIFF_LISTED or len(new) > DIFF_LISTED:
        out.append(f"  … the first {DIFF_LISTED} of each listed")
    return out


def lines(p: Plan, queued: Mapping[str, later_queue.Entry], verbose: bool = False) -> list[str]:
    """The dry run's summary of *p*; *queued*: the queue's entries by release group."""
    held = sum(1 for _, r in p.later if later_queue.held(r))
    moved = sum(1 for _, r in p.later if r["release_group"] in queued)
    out = [f"Later: {len(p.later)} album(s) to queue in the later store's order, ahead of the queue's "
           f"{len(queued)}: {held} available now, {len(p.later) - held} pending"
           + (f"; {moved} on the queue already move to their store place" if moved else ""),
           f"Keep: {len(p.keep)} album(s) to request (tracklists read from MusicBrainz at --apply), "
           f"{len(p.requested)} requested already"]
    for name in (LATER, KEEP):
        if p.unresolved[name]:
            out.append(f"{name}: {len(p.unresolved[name])} album(s) have no release group yet and are left out:")
            out += [f"  [NO-RG] {name}: {label(k, r)}: {r.get('status')}, {r.get('unresolved')}"
                    for k, r in p.unresolved[name]]
        if p.unknown[name]:
            out.append(f"{name}: {len(p.unknown[name])} album(s) on the store have no album row and are left out:")
            out += [f"  [NO-ROW] {name}: {a}" for a in p.unknown[name]]
    if verbose:
        out += [f"  [LATER] {n}. {label(k, r)}" for n, (k, r) in enumerate(p.later, 1)]
        out += [f"  [KEEP] {label(k, r)}" for k, r in p.keep]
    return out


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _songs(name: str) -> list[dict]:
    playlist = store.load(name)
    if playlist is None:
        raise SystemExit(f"The {name} store can't be read: run music-cutover while {name} is in playlists.conf")
    return playlist.songs


def _plan(conn: psycopg.Connection) -> tuple[statedb.AlbumRows, list[later_queue.Entry], Plan]:
    rows = statedb.load_albums(conn)
    return rows, later_queue.load(conn), plan(rows, _songs(LATER), _songs(KEEP),
                                              (r.release_group for r in req.load(conn)))


def cut_over(conn: psycopg.Connection, apply: bool, get: req.Get | None = None, verbose: bool = False) -> None:
    rows, entries, p = _plan(conn)
    for line in lines(p, {e.release_group: e for e in entries}, verbose):
        logger.info("%s", line)
    for line in compare(current_m3u(), m3u_lines(queue_after(entries, rows, p.later))):
        logger.info("%s", line)
    if not apply:
        logger.info("Dry run; --apply to write")
        return
    # MusicBrainz first, a read a second: the locks are held only for the writes.
    found, skipped = tracklists(get or req.musicbrainz(), p.keep)
    for line in skipped:
        logger.info("%s", line)
    from music_fetch.state_import import _locks  # noqa: PLC0415

    with _locks(True), conn.transaction():
        rows, entries, p = _plan(conn)  # again: a tick may have changed the rows meanwhile
        keep = {r["release_group"]: (k, r) for k, r in p.keep}
        marker = statedb.get(conn, statedb.CUTOVER) or {"queue": {}, "requests": []}
        queue = write_queue(conn, p.later, marker["queue"])
        added = [f.release_group for f, _, _ in found if f.release_group in keep
                 and req.add(conn, f, "cutover:keep", *keep[f.release_group])]
        statedb.put(conn, statedb.CUTOVER, {
            "at": req.iso(datetime.now().astimezone()), "by": req.caller(),
            "queue": {**marker["queue"], **queue},
            "requests": list(dict.fromkeys([*marker["requests"], *added]))})
    logger.info("Applied: %d album(s) on Later in the later store's order, %d Keep request(s) added%s", len(queue),
                len(added), f", {len(skipped)} skipped (--apply again for them)" if skipped else "")


def revert(conn: psycopg.Connection, apply: bool) -> None:
    marker = statedb.get(conn, statedb.CUTOVER)
    if marker is None:
        logger.info("No cutover to revert (no %s marker)", statedb.CUTOVER)
        return
    added = [g for g, old in marker["queue"].items() if old is None]
    moved = {g: old for g, old in marker["queue"].items() if old is not None}
    logger.info("Revert: %d Later entr(ies) to remove, %d to move back, %d Keep request(s) to remove",
                len(added), len(moved), len(marker["requests"]))
    if not apply:
        logger.info("Dry run; --apply to write")
        return
    from music_fetch.state_import import _locks  # noqa: PLC0415

    with _locks(True), conn.transaction():
        rows = statedb.load_albums(conn)
        on = [e for e in later_queue.load(conn) if e.release_group in set(added)]
        removed = later_queue.remove(conn, on, rows, later_queue.requests_by_group(conn))
        for group, old in moved.items():
            conn.execute("UPDATE later_queue SET added_at = %s::timestamptz, available_at = %s::timestamptz "
                         "WHERE release_group = %s",
                         (old["added_at"], old["available_at"], group))
        dropped = conn.execute("DELETE FROM album_requests WHERE release_group = ANY(%s::uuid[])",
                               (marker["requests"],)).rowcount
        statedb.delete(conn, statedb.CUTOVER)
    logger.info("Reverted: %d Later entr(ies) removed, %d moved back, %d Keep request(s) removed", removed,
                len(moved), dropped)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="music-cutover",
        description="Move Later onto the Later queue and Keep into album requests, once (homelab#2478); "
                    "dry run unless --apply")
    parser.add_argument("--apply", action="store_true", help="write the queue entries and the requests")
    parser.add_argument("--revert", action="store_true", help="take off what an earlier --apply wrote")
    parser.add_argument("--verbose", action="store_true", help="list every album queued and requested")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    if not statedb.in_db():
        raise SystemExit("The state is not in the music database (music-state-import): nothing to cut over")
    with statedb.connect() as conn:
        if args.revert:
            revert(conn, args.apply)
        else:
            cut_over(conn, args.apply, verbose=args.verbose)

"""``music-requests``: list and add album requests (homelab#2478, :mod:`music_fetch.album_requests`).

``add`` is a dry run unless ``--apply``.  The dry run reads each release
group's tracklist from MusicBrainz and says what the next album tick does with
it, and is what the Music MCP shows before an apply and compares again before
applying, so it prints nothing that changes between two runs of the same
request.  The release groups follow ``--``: ``<release group>`` or
``<release group>=<preferred release>``, each an MBID or a musicbrainz.org URL.
``--later`` also appends them to the Later queue (:mod:`music_fetch.later_queue`),
in the order given.  ``search -- <query>`` lists the release groups
MusicBrainz finds for a query the caller typed, with their releases, flagging
demo, live, remix and compilation ones; it reads nothing but MusicBrainz.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping

import psycopg

from music_fetch import album_requests as req
from music_fetch import later_queue
from music_fetch import state as statedb
from music_fetch.overrides_cli import describe

logger = logging.getLogger(__name__)

STATUSES = (req.PENDING, "wanted", "dry-run", "grabbed", "missing", "have", "imported", "failed", "fallback", "filled")


def parse(specs: list[str]) -> list[tuple[str, str | None]]:
    """``(release group, preferred release or None)`` per spec, in order, each once."""
    out: dict[str, str | None] = {}
    for spec in specs:
        group, _, release = spec.strip().partition("=")
        try:
            pair = (req.mbid(group, "release-group"), req.mbid(release, "release") if release.strip() else None)
        except ValueError as exc:
            raise req.Refused(str(exc)) from exc
        if pair[0] in out and out[pair[0]] != pair[1]:
            raise req.Refused(f"release group {pair[0]} is given twice, with different preferred releases")
        out[pair[0]] = pair[1]
    if not out:
        raise req.Refused("name at least one release group")
    if len(out) > req.MAX_PER_CALL:
        raise req.Refused(f"{len(out)} release groups: at most {req.MAX_PER_CALL} a call")
    return list(out.items())


def _search(found: req.Tracklist, key: str | None, record: Mapping | None) -> str:
    from music_fetch import albums  # noqa: PLC0415

    search = albums.plan(key or found.release_group, dict(record or found.fields()), {})
    return f'"{search.queries[0]}"{search.note}' if search.queries else "nothing: no readable words ([NOWORDS])"


def effect(found: req.Tracklist, key: str | None, record: dict | None) -> str:
    """What the next album tick does with the request."""
    from music_fetch import albums  # noqa: PLC0415

    if record is None:
        return (f"none has the release group yet: the next album tick queues a new album (wanted) and searches "
                f"Usenet for {_search(found, None, None)}")
    status = record.get("status")
    album = describe(key or "", record)
    held = status in (albums.IMPORTED, albums.HAVE, albums.FILLED)
    if albums._fed(record):
        if status in albums.WAITING:
            wanted = sum(len(t) for t in (record.get("playlists") or {}).values())
            return (f"{album}: the next album tick makes it a request: the whole album ({len(found.tracks)} "
                    f"track(s)) is wanted, not only its {wanted} wanted track(s)")
        return f"{album}: the next album tick makes it a request; it stays {status}"
    if albums._requested(record):
        return f"{album}: a request's already"
    on = ", ".join(p for p in record.get("playlists") or {} if p != req.SOURCE) or "no playlist"
    return (f"{album}: on {on}; " + ("the library holds it, nothing is downloaded" if held else "it stays as it is")
            + ", and it is kept when it leaves its playlists")


def _later_line(group: str, record: dict | None, queued: dict[str, int | None]) -> str:
    """What ``--later`` does with release group *group*: *queued* is Later's
    release groups, each with its position (None while pending)."""
    if group in queued:
        position = queued[group]
        return "  Later: on it already, " + (f"position {position}" if position else "pending its import")
    if later_queue.held(record):
        return "  Later: appended; the library holds it, so it joins the end of Later at the next scan"
    return "  Later: appended; it joins the end of Later once the library holds it whole"


def add_dry_run(conn: psycopg.Connection, rows: statedb.AlbumRows, specs: list[tuple[str, str | None]],
                get: req.Get, later: bool = False
                ) -> tuple[list[tuple[req.Tracklist, str | None, dict | None]], list[str], list[str]]:
    """The requests ``--apply`` would add (each with the album row that has
    its release group, if any), the release groups it would append to Later
    (*later*), and the dry run's lines.  A release group requested already is
    listed and left as it is, and one on Later already stays where it is."""
    have = {r.release_group: r for r in req.load(conn)}
    queued: dict[str, int | None] = {}
    if later:
        entries = later_queue.load(conn)
        available = [e.release_group for e in entries if e.available_at is not None]
        queued = {e.release_group: None for e in entries} | {g: n for n, g in enumerate(available, 1)}
        queued.pop(None, None)  # an entry named by ref alone: no release group to match
    out: list[tuple[req.Tracklist, str | None, dict | None]] = []
    queue: list[str] = []
    lines: list[str] = []
    for n, (group, preferred) in enumerate(specs, 1):
        head = f"Request {n} of {len(specs)}:"
        hit = req.find_row(rows, group)
        if later:
            if group not in queued:
                queue.append(group)
        if group in have:
            old = have[group]
            lines.append(f"{head} {old.artist} — {old.name} [release group {group}]: requested already, by "
                         f"{old.requested_by} at {req.iso(old.requested_at)}; nothing changes")
            if later:
                lines.append(_later_line(group, hit[1] if hit else None, queued))
            continue
        try:
            found = req.tracklist(get, group, preferred)
        except OSError as exc:  # urllib's errors: MusicBrainz down or refusing
            raise req.Refused(f"reading release group {group} from MusicBrainz failed: {exc}") from exc
        isrcs = sum(1 for t in found.tracks if t[3])
        key, record = hit if hit else (None, None)
        lines += [f"{head} {found.artist} — {found.name} [release group {group}; {found.album_type}"
                  + (f"; {found.year}" if found.year else "") + "]",
                  f"  Tracklist: {len(found.tracks)} track(s), {isrcs} with an ISRC, from release {found.release}: "
                  f"{found.release_label}; {found.how}",
                  f"  Album: {effect(found, key, record)}"]
        if later:
            lines.append(_later_line(group, record, queued))
        out.append((found, key, record))
    if out:
        lines.append(f"{len(out)} request(s) to add; a request Usenet misses stays missing (no spotdl fallback yet)")
    if queue:
        lines.append(f"{len(queue)} album(s) to append to Later, in this order")
    return out, lines, queue


def search_lines(found: list[req.Found], query: str) -> list[str]:
    """``search``'s lines: each release group, its flags, then its releases (earliest first)."""
    lines: list[str] = []
    for n, hit in enumerate(found, 1):
        group = hit.group
        types = " + ".join([group.get("primary-type") or "?", *(group.get("secondary-types") or ())])
        year = f"; {group['first-release-date'][:4]}" if group.get("first-release-date") else ""
        lines.append(f"{n}. {req._credit(group.get('artist-credit'))} — {group.get('title', '')} "
                     f"[release group {hit.id}; {types}{year}]"
                     + (f" ({group['disambiguation']})" if group.get("disambiguation") else ""))
        if hit.flags:
            lines.append(f"  Flagged: {', '.join(hit.flags)}: not a studio album")
        lines.append(f"  {hit.release_count} release(s)" + (":" if hit.releases else ""))
        for release in hit.releases[:req.SEARCH_RELEASES]:
            flags = req.release_flags(release)
            lines.append(f"  - {release['id']}: {req._release_label(release)}, {req._track_count(release)} track(s)"
                         + (f" [flagged: {', '.join(flags)}]" if flags else ""))
        if hit.release_count > req.SEARCH_RELEASES:
            lines.append(f"  … and {hit.release_count - req.SEARCH_RELEASES} more")
    lines.append(f"{len(found)} release group(s) for {query!r}"
                 + ("; request one with request_album (a release ID above as preferred_release only when the "
                    "edition matters)" if found else ""))
    return lines


def _connect() -> psycopg.Connection:
    if not statedb.in_db():
        raise req.Refused("the state is not in the music database yet (music-state-import)")
    return statedb.connect()


def _listed(entry: dict) -> str:
    album = entry["album"]
    where = (f"album {album['key']}, {album['status']} ({album['origin']})" if album
             else "pending: the next album tick queues it")
    preferred = f"; preferred release {entry['preferred_release']}" if entry["preferred_release"] else ""
    return (f"{entry['artist']} — {entry['name']} [release group {entry['release_group']}{preferred}]: {where}; "
            f"requested by {entry['requested_by']} at {entry['requested_at']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-requests",
        description="Album requests by MusicBrainz release group (homelab#2478); add is a dry run unless --apply")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="every request, with its album's status")
    p.add_argument("--status", choices=STATUSES)
    p = sub.add_parser("add", help="request albums by release group")
    p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
    p.add_argument("--later", action="store_true", help="also append them to Later, in the order given")
    p.add_argument("specs", nargs="+", metavar="RELEASE_GROUP[=RELEASE]",
                   help="a release group MBID or URL, optionally =<preferred release MBID or URL>")
    p = sub.add_parser("search", help="find release groups on MusicBrainz by a typed query")
    p.add_argument("--limit", type=int, default=req.SEARCH_LIMIT,
                   help=f"release groups to list, 1 to {req.SEARCH_MAX} (default {req.SEARCH_LIMIT})")
    p.add_argument("query", nargs="+", help="what the user typed: an artist and album, or MusicBrainz search syntax")
    return parser


def run(args: argparse.Namespace, get: req.Get | None = None) -> None:
    if args.command == "search":
        query = " ".join(" ".join(args.query).split())
        if not query or len(query) > req.QUERY_MAX:
            raise req.Refused(f"the query must be 1 to {req.QUERY_MAX} characters")
        if not 1 <= args.limit <= req.SEARCH_MAX:
            raise req.Refused(f"--limit must be 1 to {req.SEARCH_MAX}")
        try:
            found = req.search(get or req.musicbrainz(), query, args.limit)
        except OSError as exc:  # urllib's errors: MusicBrainz down or refusing
            raise req.Refused(f"searching MusicBrainz failed: {exc}") from exc
        for line in search_lines(found, query):
            logger.info("%s", line)
        return
    specs = parse(args.specs) if args.command == "add" else []
    with _connect() as conn:
        rows = statedb.load_albums(conn)
        if args.command == "list":
            found = req.listing(conn, rows, args.status)
            for entry in found:
                logger.info("%s", _listed(entry))
            logger.info("%d request(s)", len(found))
            return
        out, lines, queue = add_dry_run(conn, rows, specs, get or req.musicbrainz(), args.later)
        for line in lines:
            logger.info("%s", line)
        if not args.apply:
            logger.info("Dry run; --apply to write")
            return
        by = req.caller()
        with conn.transaction():
            added = sum(req.add(conn, found, by, key, record) for found, key, record in out)
            appended = later_queue.add(conn, queue, rows, later_queue.requests_by_group(conn)) if queue else 0
        logger.info("Applied: %d request(s) added%s%s", added,
                    f", {len(out) - added} requested meanwhile" if added < len(out) else "",
                    f"; {appended} album(s) appended to Later" if args.later else "")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        run(args)
    except (req.Refused, statedb.Unavailable) as exc:
        logger.error("%s", exc)
        sys.exit(1)

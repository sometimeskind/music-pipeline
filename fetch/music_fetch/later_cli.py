"""``music-later``: list Later and take albums off it (homelab#2478, :mod:`music_fetch.later_queue`).

``remove`` is a dry run unless ``--apply``.  The Music MCP's
``remove_from_later`` shows the dry run before an apply and compares it again
before applying, so it prints nothing that changes between two runs.  The
albums to remove are named by release group after ``--`` (an MBID or a
musicbrainz.org URL), by album with ``--album`` (as ``find_albums`` names one:
its key, a Spotify album ID or URL, ``artist — album`` or a part matching one
album; homelab#2534), which reaches an entry held by ref too, or ``--through N``
takes Later's first N albums in ``.m3u`` order: the ones listened to.  Albums are appended by
``music-requests add --later``.  ``auto-requests`` lists the albums the next
album tick requests automatically (homelab#2535, :mod:`music_fetch.later_requests`):
read-only, and it reads nothing but the music database.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

import psycopg

from music_fetch import album_requests as req
from music_fetch import later_queue as later
from music_fetch import later_requests
from music_fetch import overrides as ov
from music_fetch import state as statedb
from music_fetch.album_requests_cli import _connect
from music_fetch.overrides_cli import select_album

logger = logging.getLogger(__name__)


def parse(specs: list[str]) -> list[str]:
    """The release groups *specs* name, in order, each once."""
    out: dict[str, None] = {}
    for spec in specs:
        try:
            out[req.mbid(spec, "release-group")] = None
        except ValueError as exc:
            raise req.Refused(str(exc)) from exc
    return list(out)


def select(rows: statedb.AlbumRows, queries: list[str]) -> list[tuple[str, dict]]:
    """The album rows *queries* name, in order, each once: each query must name
    exactly one row (:func:`overrides_cli.select_album`), else nothing is done."""
    out: dict[str, dict] = {}
    for query in queries:
        try:
            key, record = select_album(rows, query)
        except ov.Refused as exc:
            raise req.Refused(f"--album: {exc}") from exc
        out.setdefault(key, record)
    return list(out.items())


def remove_dry_run(conn: psycopg.Connection, rows: statedb.AlbumRows, groups: list[str],
                   through: int | None, albums: Sequence[str] = ()) -> tuple[list[later.Entry], list[str]]:
    """The entries ``--apply`` would remove, and the dry run's lines: the
    first *through* available albums, then *groups*, then *albums* in order.
    A release group or album not on Later is listed and left as it is.  An
    entry named by ref alone (queued by the cutover before its row had a
    release group) is reached by its album (homelab#2534) or by *through*,
    never by release group."""
    if not groups and not albums and not through:
        raise req.Refused("name at least one release group or --album, or --through")
    selected = select(rows, list(albums))
    entries = later.load(conn)
    available = [e for e in entries if e.available_at is not None]
    position = {e.id: n for n, e in enumerate(available, 1)}
    if through and through > len(available):
        raise req.Refused(f"--through {through}: Later lists {len(available)} album(s)")
    chosen = {e.id: e for e in available[:through or 0]}
    on = {e.release_group: e for e in entries if e.release_group}
    requests = later.requests_by_group(conn)
    index = later.Index(rows)
    lines: list[str] = []
    for group in groups:
        if group in on:
            chosen.setdefault(on[group].id, on[group])
        else:
            lines.append(f"{_named(group, index.groups.get(group), requests)}: not on Later; nothing changes")
    # An album's row is on Later by release group or by ref: the entries whose row it is.
    # Two entries are one row only between a release group arriving and the regen merging them.
    held: dict[str, list[later.Entry]] = {}
    for entry in entries:
        if (found := index.find(entry)) is not None:
            held.setdefault(found[0], []).append(entry)
    for key, record in selected:
        if key in held:
            for entry in held[key]:
                chosen.setdefault(entry.id, entry)
        else:
            lines.append(f"{_named(record.get('release_group'), (key, record), requests, later.ref(record))}"
                         f": not on Later; nothing changes")
    for n, entry in enumerate(chosen.values(), 1):
        where = (f"position {position[entry.id]} of {len(available)}" if entry.id in position
                 else "pending: not in the .m3u yet")
        lines.append(f"Remove {n} of {len(chosen)}: "
                     f"{_named(entry.release_group, index.find(entry), requests, entry.ref)}: {where}")
    if chosen:
        lines.append(f"{len(chosen)} album(s) leave Later at the next scan's regen; their files stay in the library")
    return list(chosen.values()), lines


def _named(group: str | None, found: tuple[str, dict] | None, requests: dict, ref: str | None = None) -> str:
    """``artist — name [release group …]``, or the release group (or ref) alone when nothing names it."""
    label = later.label(group, found, requests, ref)
    if label.startswith(("release group ", "album ref ")):
        return label[0].upper() + label[1:]  # not capitalize(): a Spotify ID's case stays
    return f"{label} [release group {group}]" if group else f"{label} [album ref {ref}, no release group yet]"


def _listed(entry: dict) -> str:
    album = entry["album"]
    where = f"album {album['key']}, {album['status']} ({album['origin']})" if album else "pending: no album row yet"
    head = f"{entry['position']}." if entry["position"] else "-"
    name = f"{entry['artist']} — {entry['name']}" if entry["name"] else "?"
    named = (f"release group {entry['release_group']}" if entry["release_group"]
             else f"album ref {entry['ref']}, no release group yet")
    return (f"{head} {name} [{named}]: {entry['state']}; {where}; "
            f"added at {entry['added_at']}" + (f", available at {entry['available_at']}" if entry["available_at"] else ""))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-later",
        description="The Later queue (homelab#2478); remove is a dry run unless --apply")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every album on Later, in .m3u order, then the pending ones")
    sub.add_parser("auto-requests", help="the incomplete Later albums the album tick requests automatically "
                                         "(read-only)")
    p = sub.add_parser("remove", help="take albums off Later (their files stay)")
    p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
    p.add_argument("--through", type=int, metavar="N", help="Later's first N albums, in .m3u order")
    p.add_argument("--album", action="append", default=[], metavar="QUERY",
                   help="an album, held on Later by release group or by ref: its key, a Spotify album ID or URL, "
                        "'artist — album', or a part matching one album (repeatable)")
    p.add_argument("groups", nargs="*", metavar="RELEASE_GROUP", help="a release group MBID or URL")
    return parser


def run(args: argparse.Namespace) -> None:
    groups = parse(args.groups) if args.command == "remove" else []
    if args.command == "remove" and args.through is not None and args.through < 1:
        raise req.Refused("--through must be 1 or more")
    with _connect() as conn:
        rows = statedb.load_albums(conn)
        if args.command == "list":
            found = later.listing(conn, rows)
            for entry in found:
                logger.info("%s", _listed(entry))
            logger.info("%d album(s) on Later, %d pending", len(found),
                        sum(1 for e in found if e["state"] == later.PENDING))
            return
        if args.command == "auto-requests":
            for line in later_requests.lines(later_requests.plan(conn, rows)):
                logger.info("%s", line)
            return
        out, lines = remove_dry_run(conn, rows, groups, args.through, args.album)
        for line in lines:
            logger.info("%s", line)
        if not args.apply:
            logger.info("Dry run; --apply to write")
            return
        with conn.transaction():
            removed = later.remove(conn, out, rows, later.requests_by_group(conn))
        logger.info("Applied: %d album(s) removed from Later%s", removed,
                    f", {len(out) - removed} removed meanwhile" if removed < len(out) else "")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        run(args)
    except (req.Refused, statedb.Unavailable) as exc:
        logger.error("%s", exc)
        sys.exit(1)

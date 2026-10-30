"""``music-later``: list Later and take albums off it (homelab#2478, :mod:`music_fetch.later_queue`).

``remove`` is a dry run unless ``--apply``.  The Music MCP's
``remove_from_later`` shows the dry run before an apply and compares it again
before applying, so it prints nothing that changes between two runs.  The
albums to remove are named by release group after ``--`` (an MBID or a
musicbrainz.org URL), or ``--through N`` takes Later's first N albums in
``.m3u`` order: the ones listened to.  Albums are appended by
``music-requests add --later``.
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from music_fetch import album_requests as req
from music_fetch import later_queue as later
from music_fetch import state as statedb
from music_fetch.album_requests_cli import _connect

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


def remove_dry_run(conn: psycopg.Connection, rows: statedb.AlbumRows, groups: list[str],
                   through: int | None) -> tuple[list[later.Entry], list[str]]:
    """The entries ``--apply`` would remove, and the dry run's lines: the
    first *through* available albums, then *groups* in order.  A release
    group not on Later is listed and left as it is."""
    if not groups and not through:
        raise req.Refused("name at least one release group, or --through")
    entries = later.load(conn)
    available = [e for e in entries if e.available_at is not None]
    position = {e.release_group: n for n, e in enumerate(available, 1)}
    if through and through > len(available):
        raise req.Refused(f"--through {through}: Later lists {len(available)} album(s)")
    chosen = {e.release_group: e for e in available[:through or 0]}
    on = {e.release_group: e for e in entries}
    requests = later.requests_by_group(conn)
    index = later.by_group(rows)
    lines: list[str] = []
    for group in groups:
        if group in on:
            chosen.setdefault(group, on[group])
        else:
            lines.append(f"{_named(group, index.get(group), requests)}: not on Later; nothing changes")
    for n, entry in enumerate(chosen.values(), 1):
        where = (f"position {position[entry.release_group]} of {len(available)}" if entry.release_group in position
                 else "pending: not in the .m3u yet")
        lines.append(f"Remove {n} of {len(chosen)}: "
                     f"{_named(entry.release_group, index.get(entry.release_group), requests)}: {where}")
    if chosen:
        lines.append(f"{len(chosen)} album(s) leave Later at the next scan's regen; their files stay in the library")
    return list(chosen.values()), lines


def _named(group: str, found: tuple[str, dict] | None, requests: dict) -> str:
    """``artist — name [release group …]``, or the release group alone when nothing names it."""
    label = later.label(group, found, requests)
    return f"{label} [release group {group}]" if not label.startswith("release group ") else label.capitalize()


def _listed(entry: dict) -> str:
    album = entry["album"]
    where = f"album {album['key']}, {album['status']} ({album['origin']})" if album else "pending: no album row yet"
    head = f"{entry['position']}." if entry["position"] else "-"
    name = f"{entry['artist']} — {entry['name']}" if entry["name"] else "?"
    return (f"{head} {name} [release group {entry['release_group']}]: {entry['state']}; {where}; "
            f"added at {entry['added_at']}" + (f", available at {entry['available_at']}" if entry["available_at"] else ""))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-later",
        description="The Later queue (homelab#2478); remove is a dry run unless --apply")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every album on Later, in .m3u order, then the pending ones")
    p = sub.add_parser("remove", help="take albums off Later (their files stay)")
    p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
    p.add_argument("--through", type=int, metavar="N", help="Later's first N albums, in .m3u order")
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
        out, lines = remove_dry_run(conn, rows, groups, args.through)
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

"""``music-album-blocklist``: blocklist one release for one album by hand, or
take such an entry off again (homelab#2750, :mod:`music_fetch.blocklist`).

A release the matcher accepts can still be the wrong one: a plain title
(``Artist-Album.1987.FLAC``) that holds another album's files passes every
rule, and a ``reimport`` reset would grab it again whenever it ranks first.
``add`` appends its GUID to the album's ``blocklist`` and records a
``blocklisted`` event whose reason begins :data:`blocklist.BY_HAND`: a
:data:`blocklist.HAND` entry, which blocks every copy of its title on any
indexer and which no reset clears, the last candidate included
(:func:`blocklist.stays`).  ``remove`` takes a hand entry off, with an
``unblocklisted`` event; no other entry, since what a reset leaves on the list
it leaves there on purpose.

The release is named by its GUID or its exact title, as the album's own
history gives them: ``album_history``'s grabs and failures (``data.guid``,
``data.release``), or ``get_album``'s ``candidate`` and ``blocklisted``.  A
release the album never grabbed isn't one it can be refused, so it isn't
taken.  Blocklisting the release the album holds changes nothing until a
reset: the album stays as it is, and ``reset_album`` with ``reimport=true``
then grabs another release or logs ``[STAYS]``.  A release in flight is
refused: its import outcome would overwrite the reason.

Every write is a dry run unless ``--apply``, and the Music MCP compares the
dry run again before an apply, so it prints nothing that changes between two
runs of one request.  The apply saves the record and writes its event in one
transaction, under the ``albums`` slot the tick holds.  Free text (the album,
the release) follows ``--``: it never parses as a flag.
"""

from __future__ import annotations

import argparse
import logging
import sys

from music_fetch import albums, blocklist, events, overrides
from music_fetch import state as statedb
from music_fetch.usenet import Release

logger = logging.getLogger(__name__)

ADD, REMOVE = "add", "remove"
# How many of the album's releases a refusal lists.
LISTED = 20


class Refused(RuntimeError):
    """A request that can't be done (no such album or release, a release in flight): exit 1 with the reason."""


def select_album(state: albums.State, query: str) -> tuple[str, dict]:
    """The one album *query* names (:func:`albums.find_albums`)."""
    hits = albums.find_albums(state.albums, albums._album_ref(query.strip()))
    if len(hits) != 1:
        listed = "".join(f"\n  {k}: {r.get('artist')} — {r.get('name')} ({r.get('status')})" for k, r in hits[:LISTED])
        raise Refused(f"{len(hits)} album(s) match {query!r}{listed}")
    return hits[0]


def releases(record: dict, rows: list) -> dict[str, dict]:
    """Every release the album's events or record name, by GUID, first seen first: its ``title`` and ``size``."""
    found: dict[str, dict] = {}

    def note(guid: str | None, title: str | None, size: int | None) -> None:
        if not guid:
            return
        release = found.setdefault(guid, {"title": None, "size": None})
        release["title"] = release["title"] or title or None
        release["size"] = release["size"] or size or None

    for _, _, data in rows:
        note(data.get("guid"), data.get("release"), data.get("size"))
    for copy in (record.get("candidate"), record.get("best_copy")):
        if copy:
            note(copy.get("guid"), copy.get("title"), copy.get("size"))
    return found


def _named(found: dict[str, dict], record: dict, release: str) -> str | None:
    """The GUID *release* names: itself, or the one release of that exact title
    (several copies: the candidate's, else the one first seen last)."""
    if release in found:
        return release
    same = [guid for guid, r in found.items() if r["title"] == release]
    if not same:
        return None
    candidate = (record.get("candidate") or {}).get("guid")
    return candidate if candidate in same else same[-1]


def _unknown(key: str, release: str, found: dict[str, dict]) -> Refused:
    listed = "".join(f"\n  {r['title'] or '(no title recorded)'} [{guid}]" for guid, r in list(found.items())[:LISTED])
    return Refused(f"no release in album {key}'s history is {release!r}: give its GUID or exact title as "
                   f"album_history's grab or get_album's candidate shows it" + (f". Its releases:{listed}" if listed
                                                                                 else "; it has grabbed none"))


def _album_line(key: str, record: dict) -> str:
    return (f"Album: {record.get('artist', '')} — {record.get('name', '')} [{key}; {record.get('status')}; "
            f"{len(record.get('blocklist') or ())} blocklisted]")


def _release_line(guid: str, title: str | None, size: int | None) -> str:
    return f"Release: {title or '(no title recorded)'}{f' ({size / 1e6:.0f} MB)' if size else ''} [{guid}]"


def _hand_entry(guid: str, title: str | None, size: int | None, reason: str) -> dict:
    """The entry :func:`blocklist.entries` will read once the event is written."""
    entry = {"guid": guid, "title": title, "size": size, "reason": f"{blocklist.BY_HAND}{reason}", "at": None}
    entry["kind"] = blocklist.kind(entry)
    entry["every_copy"] = bool(title)
    return entry


def _effect(record: dict, guid: str) -> str:
    status = record.get("status")
    if status in (albums.WANTED, albums.MISSING, albums.DRY_RUN):
        return "Effect: the album's next search skips it."
    held = (record.get("candidate") or {}).get("guid") == guid
    return (f"Effect: none until the album is searched again: it is {status}"
            f"{', and was imported from this release' if held and status == albums.IMPORTED else ''}. "
            "No reset clears this entry: reset_album with reimport=true then grabs the best other release the "
            "matcher accepts, or with none left logs [STAYS] and leaves the album as it is.")


def add_dry_run(state: albums.State, key: str, record: dict, rows: list, release: str,
                reason: str) -> tuple[list[str], dict | None, str | None]:
    """The dry run of blocklisting *release* by hand: its lines, the entry to
    write (None: nothing to do), and the entry's reason before (None: not on the list)."""
    found = releases(record, rows)
    guid = _named(found, record, release)
    if guid is None:
        raise _unknown(key, release, found)
    title, size = found[guid]["title"], found[guid]["size"]
    if record.get("status") == albums.GRABBED and (record.get("candidate") or {}).get("guid") == guid:
        raise Refused(f"{title or guid} is in flight for album {key} (grabbed): blocklist it once its import is "
                      "done, as its outcome would overwrite the reason")
    lines = [_album_line(key, record), _release_line(guid, title, size)]
    listed = blocklist.entries(record, rows)
    hand = [e for e in listed if e["kind"] == blocklist.HAND]
    if covering := next((e for e in hand if Release(guid=guid, title=title or "", size=0, indexer_id=0,
                                                       download_url="", categories=[]) in blocklist.blocked([e])), None):
        lines.append(f"Blocklist: blocklisted by hand already: {blocklist.describe(covering)}. Nothing to change "
                     "(unblocklist_release first for another reason)")
        return lines, None, None
    old = next((e for e in listed if e["guid"] == guid), None)
    entry = _hand_entry(guid, title, size, reason)
    before = "not on it" if old is None else f"on it as {old['reason']} ({blocklist.reach(old)})"
    lines.append(f"Blocklist: {before} → {entry['reason']} ({blocklist.reach(entry)})")
    lines.append(_effect(record, guid))
    return lines, entry, None if old is None else old["reason"]


def remove_dry_run(state: albums.State, key: str, record: dict, rows: list, release: str) -> tuple[list[str], list[dict]]:
    """The dry run of taking *release*'s hand entries off: its lines and those entries."""
    listed = blocklist.entries(record, rows)
    named = [e for e in listed if release in (e["guid"], e["title"])]
    if not named:
        raise Refused(f"{release!r} is not on album {key}'s blocklist (get_album's blocklisted lists it)")
    gone = [e for e in named if e["kind"] == blocklist.HAND]
    if not gone:
        raise Refused(f"{blocklist.describe(named[0])} is not blocklisted by hand, so this doesn't take it off: a "
                      "reset clears a wrong release, and nothing clears a dead download")
    lines = [_album_line(key, record)]
    stay = [e for e in listed if e not in gone]
    search = albums.plan(key, record, albums._search_overrides(state))
    imported = albums._taken(state, key)[1]
    for e in gone:
        lines.append(_release_line(e["guid"], e["title"], e["size"]))
        _, note = albums._verdict(e, record, search, imported, blocklist.blocked(stay))
        lines.append(f"Blocklist: {e['reason']} ({blocklist.reach(e)}) → off it; {note}")
    status = record.get("status")
    if status in (albums.WANTED, albums.MISSING, albums.DRY_RUN):
        lines.append("Effect: the album's next search may grab it again.")
    else:
        lines.append(f"Effect: none until the album is searched again (it is {status}): a reset may then grab it.")
    return lines, gone


def _album_data(key: str, record: dict) -> tuple[str | None, str | None, str, str]:
    return (statedb.primary_spotify(record), albums._release_group(record),
            f"{record.get('artist')} — {record.get('name')}", record.get("origin") or "album")


def write_add(state: albums.State, key: str, record: dict, entry: dict, was: str | None) -> None:
    """Append *entry*'s GUID (unless it is there) and record its ``blocklisted`` event, in one transaction."""
    by = overrides.caller()
    listed = record.setdefault("blocklist", [])
    if entry["guid"] not in listed:
        listed.append(entry["guid"])
    spotify, group, album, origin = _album_data(key, record)
    message = (f"[BLOCKLIST] {albums._label(record)} ({key}): {entry['title'] or entry['guid']} blocklisted by "
               f"{by}: {entry['reason'][len(blocklist.BY_HAND):]}")
    with statedb.session() as conn, conn.transaction():
        statedb.save_albums(conn, state)
        events.insert(conn, "blocklisted", message, source="albums", spotify_album_id=spotify, release_group=group,
                      data={"album": album, "key": key, "origin": origin, "release": entry["title"],
                            "guid": entry["guid"], "size": entry["size"], "reason": entry["reason"], "was": was})
    logger.info("%s", message)


def write_remove(state: albums.State, key: str, record: dict, gone: list[dict]) -> None:
    """Take *gone*'s GUIDs off the blocklist, each with its ``unblocklisted`` event, in one transaction."""
    by = overrides.caller()
    guids = {e["guid"] for e in gone}
    record["blocklist"] = [g for g in record.get("blocklist") or () if g not in guids]
    spotify, group, album, origin = _album_data(key, record)
    with statedb.session() as conn, conn.transaction():
        statedb.save_albums(conn, state)
        for e in gone:
            message = (f"[UNBLOCKLIST] {albums._label(record)} ({key}): {e['title'] or e['guid']} off the blocklist, "
                       f"by {by} (it was blocklisted {e['reason']})")
            events.insert(conn, "unblocklisted", message, source="albums", spotify_album_id=spotify,
                          release_group=group, data={"album": album, "key": key, "origin": origin,
                                                     "release": e["title"], "guid": e["guid"], "reason": e["reason"]})
            logger.info("%s", message)


def _album_rows(key: str, record: dict) -> list:
    with statedb.session() as conn:
        return blocklist.album_events(conn, events.album_refs(key, record), albums._release_group(record))


def run(args: argparse.Namespace) -> None:
    release = args.release.strip()
    if not release:
        raise Refused("name the release: its GUID or exact title")
    with albums._state_lock(args.apply):
        state = albums.State.load()
        key, record = select_album(state, args.album)
        rows = _album_rows(key, record)
        if args.command == ADD:
            reason = " ".join(args.reason.split())
            if not reason:
                raise Refused("--reason must say why")
            lines, entry, was = add_dry_run(state, key, record, rows, release, reason)
        else:
            lines, gone = remove_dry_run(state, key, record, rows, release)
        for line in lines:
            logger.info("%s", line)
        if not args.apply:
            logger.info("Dry run; --apply to write")
            return
        if args.command == ADD:
            if entry is not None:
                write_add(state, key, record, entry, was)
        else:
            write_remove(state, key, record, gone)
        logger.info("Applied")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-album-blocklist",
        description="Blocklist one release for one album by hand, so no reset or reimport grabs it again, or take "
                    "such an entry off (homelab#2750); every write is a dry run unless --apply")
    sub = parser.add_subparsers(dest="command", required=True)
    album_help = "album key, Spotify album ID or URL, release group, or 'artist — album'"
    release_help = "the release's GUID or exact title, as album_history's grab or get_album's candidate shows it"
    p = sub.add_parser(ADD, help="blocklist a release the album grabbed; every copy of its title is refused")
    p.add_argument("--reason", required=True, help="why, kept in its event and shown by get_album")
    p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
    p.add_argument("album", help=album_help)
    p.add_argument("release", help=release_help)
    p = sub.add_parser(REMOVE, help="take a release blocklisted by hand off the album's blocklist")
    p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
    p.add_argument("album", help=album_help)
    p.add_argument("release", help=release_help)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        run(args)
    except (Refused, statedb.Unavailable, ValueError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

"""``music-overrides``: list, set and clear search overrides, holds and track
pins, and import the ConfigMap's (homelab#2484, :mod:`music_fetch.overrides`).

Every write is a dry run unless ``--apply``.  The dry run says what changes
and what the next tick or nightly does differently, and is what the Music MCP
shows before an apply and compares again before applying, so it prints nothing
that changes between two runs of the same request.  Free text (an album
query, search words) follows ``--``: it never parses as a flag.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from music_fetch import events
from music_fetch import overrides as ov
from music_fetch import state as statedb
from music_fetch.ids import spotify_key

logger = logging.getLogger(__name__)

_TRACK_ID = re.compile(r"[0-9A-Za-z]{22}")


def describe(key: str, record: Mapping) -> str:
    """One album as a dry run names it: what it is, its key, row, release group and status."""
    group = record.get("release_group") or f"no release group ({record.get('unresolved') or statedb.PENDING})"
    return (f"{record.get('artist', '')} — {record.get('name', '')} "
            f"[{key}; row {record.get('_id')}; {group}; {record.get('status')}]")


def select_album(rows: statedb.AlbumRows, query: str) -> tuple[str, dict]:
    """The one album row *query* names: its key, any Spotify album ID or URL,
    its release group, or ``artist — album`` (:func:`albums.find_albums`)."""
    from music_fetch.albums import find_albums  # noqa: PLC0415

    try:
        ref = spotify_key(query.strip(), "album")
    except ValueError as exc:
        raise ov.Refused(str(exc)) from exc
    hits = find_albums(rows.albums, ref)
    if len(hits) != 1:
        listed = "".join(f"\n  {describe(k, r)}" for k, r in hits[:20])
        raise ov.Refused(f"{len(hits)} album(s) match {query!r}{listed}")
    return hits[0]


def _connect(write: bool) -> psycopg.Connection:
    if not statedb.in_db():
        raise ov.Refused("the state is not in the music database yet (music-state-import)")
    conn = statedb.connect()
    if write and not ov.imported(conn):
        conn.close()
        raise ov.Refused("the overrides are not imported yet: run music-overrides import --apply first "
                         "(until then the tick reads album-overrides.conf and track-pins.conf)")
    return conn


def _quoted(words: str | None) -> str:
    return "none" if words is None else f'"{words}"'


def _next_search(key: str, record: dict, words: str | None) -> str:
    from music_fetch import albums  # noqa: PLC0415

    search = albums.plan(key, record, {key: words} if words else {})
    return (f'"{search.queries[0]}"{search.note}' if search.queries
            else "nothing: no readable words ([NOWORDS])")


def _when(record: dict, new_words: str | None, key: str) -> str:
    from music_fetch import albums  # noqa: PLC0415

    status = record.get("status")
    search = albums.plan(key, record, {key: new_words} if new_words else {})
    if status == albums.WANTED:
        return "at the next tick with a free slot (wanted)"
    if status == albums.MISSING:
        if albums._requery(record, search):
            return f'at the next tick (missing, last searched for "{record.get("query", "")}")'
        return f"after its missing retry ({albums.MISSING_RETRY.days} days from {record.get('searched_at')})"
    if status == albums.DRY_RUN:
        return "at the next tick that searches in ALBUM_MODE=on (dry-run)"
    return f"not while it is {status}: reset_album searches it again"


def search_dry_run(conn, rows, query: str, words: str | None, reason: str = "") -> tuple[str, dict, list[str]]:
    key, record = select_album(rows, query)
    old = ov.current(conn, ov.SEARCH, record["_id"])
    old_words = None if old is None else old.words
    lines = [f"Album: {describe(key, record)}",
             f"Search override: {_quoted(old_words)} → {_quoted(words)}"
             + (f" ({reason})" if words and reason else ""),
             f"Next search: {_next_search(key, record, old_words)} → {_next_search(key, record, words)}"]
    if old_words != words:
        lines.append(f"Searched again: {_when(record, words, key)}")
    return key, record, lines


def hold_dry_run(conn, rows, query: str, hold: bool, reason: str = "") -> tuple[str, dict, list[str], bool]:
    key, record = select_album(rows, query)
    old = ov.current(conn, ov.HOLD, record["_id"])
    before = "none" if old is None else f'held ("{old.reason}", by {old.set_by})'
    after = f'held ("{reason}")' if hold else "none"
    auto = record.get("auto_reimported_at")
    lines = [f"Album: {describe(key, record)}",
             f"Hold: {before} → {after}",
             f"Mixed at the last tick: {'yes' if key in rows.mixed else 'no'}; "
             f"reset automatically: {'once, at ' + auto if auto else 'never'}"]
    if hold:
        lines.append("Effect: the album tick never resets it for an automatic reimport (reset_album still can)")
    elif old is not None:
        lines.append("Effect: if it is mixed and was never reset automatically, the album tick may reset it "
                     "for a reimport (ALBUM_AUTO_REIMPORTS_PER_DAY)")
    return key, record, lines, old is not None


def pin_dry_run(conn, track: str, url: str | None, label: str = "", reason: str = "",
                library_db: Path = ov.LIBRARY_DB) -> tuple[ov.Pin | None, str, list[str], bool]:
    song, playlists = ov.find_track(conn, track)
    isrc = (song or {}).get("isrc") or None
    title = f"{song.get('artist', '')} — {song.get('name', '')}" if song else track
    old = next((p for p in ov.load_pins(conn) if p.spotify_track_id == track), None)
    held = ov.library_items(track, isrc, library_db)
    on = f"on {', '.join(playlists)}" if playlists else "on no playlist store"
    lines = [f"Track: {title} [{track}; ISRC {isrc or 'unknown'}; {on}]",
             f"Pin: {'none' if old is None else old.url} → "
             f"{'none' if url is None else url + (f' ({label})' if label else '')}"
             + (f"; why: {reason}" if url and reason else "")]
    if url is not None:
        lines.append(f"Library: holds it (item {', '.join(map(str, held))}): the nightly skips the pin; "
                     "pin only to replace it, with replace_item" if held else
                     "Library: lacks it: the nightly downloads it from the URL ([PIN]), ignoring its [MISS] backoff")
    pin = None if url is None else ov.Pin(track, url, isrc, label, reason)
    return pin, title, lines, old is not None


# ---------------------------------------------------------------------------
# The import
# ---------------------------------------------------------------------------


def import_plan(conn, rows: statedb.AlbumRows, albums_path: Path, pins_path: Path) -> tuple[list, list, list[str]]:
    """What ``import`` writes: (album overrides with their row or detached,
    pins, the report lines).  An entry the table holds already is skipped,
    so the import can run again; an entry no row has is imported detached and
    reported: it attaches when an album with its Spotify ID is saved."""
    by_ref = rows.by_ref()
    have = ov.load_album_overrides(conn)
    out_albums: list[tuple[ov.AlbumOverride, str | None, dict | None]] = []
    report: list[str] = []
    for o in ov.parse_albums(albums_path):
        ref = o.refs[0]
        key = by_ref.get(ref) or (ref if ref in rows.albums else None)
        record = rows.albums.get(key) if key else None
        what = f"{o.kind} {ref}" + (f' "{o.words}"' if o.words else "")
        if record is None:
            if any(h.kind == o.kind and ref in h.refs for h in have):
                report.append(f"[SKIP] {what}: imported already (detached)")
                continue
            report.append(f"[NOROW] {what}: no album row has {ref}; imported detached, it attaches when one does")
            out_albums.append((o, None, None))
            continue
        old = next((h for h in have if h.album_id == record["_id"] and h.kind == o.kind), None)
        if old is not None:
            report.append(f"[SKIP] {what}: {describe(key, record)} has one already"
                          + ("" if old.words == o.words else f" ({_quoted(old.words)}, kept)"))
            continue
        o.album_id = record["_id"]
        o.refs = ov._row_refs(record)
        report.append(f"[IMPORT] {what} → {describe(key, record)}")
        out_albums.append((o, key, record))
    have_pins = {p.spotify_track_id for p in ov.load_pins(conn)}
    out_pins = []
    for p in ov.parse_pins(pins_path):
        if p.spotify_track_id in have_pins:
            report.append(f"[SKIP] pin {p.spotify_track_id}: imported already")
            continue
        song, _ = ov.find_track(conn, p.spotify_track_id or "")
        p.isrc = (song or {}).get("isrc") or None
        title = f"{song.get('artist', '')} — {song.get('name', '')}" if song else "on no playlist store"
        report.append(f"[IMPORT] pin {p.spotify_track_id} ({title}) → {p.url}")
        out_pins.append(p)
    return out_albums, out_pins, report


def import_write(conn, out_albums, out_pins) -> None:
    """Write the import plan and the marker in one transaction, each entry with its event."""
    with conn.transaction():
        for o, key, record in out_albums:
            conn.execute("INSERT INTO album_overrides (kind, album_id, refs, words, reason, set_by) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (o.kind, o.album_id, o.refs, o.words, o.reason, o.set_by))
            what = f'search override "{o.words}"' if o.kind == ov.SEARCH else "hold"
            album = describe(key, record) if record else f"{o.refs[0]} (no album row yet)"
            events.insert(conn, "override_set", f"[OVERRIDE] {album}: {what} imported from {o.set_by}",
                          source="overrides", spotify_album_id=events.spotify_album(key or o.refs[0]),
                          release_group=(record or {}).get("release_group"),
                          data={"kind": o.kind, "key": key, "refs": o.refs, "words": o.words, "reason": o.reason})
        for p in out_pins:
            conn.execute("INSERT INTO track_pins (spotify_track_id, isrc, url, label, reason, set_by) "
                         "VALUES (%s, %s, %s, %s, %s, %s)",
                         (p.spotify_track_id, p.isrc, p.url, p.label, p.reason, p.set_by))
            events.insert(conn, "override_set", f"[PIN] {p.spotify_track_id}: pin to {p.url} imported from {p.set_by}",
                          source="overrides", data={"kind": ov.PIN, "spotify_track_id": p.spotify_track_id,
                                                    "isrc": p.isrc, "url": p.url, "label": p.label,
                                                    "reason": p.reason})
        if not ov.imported(conn):
            statedb.put(conn, statedb.OVERRIDES_IMPORTED,
                        {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "albums": len(out_albums), "pins": len(out_pins)})


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    from music_fetch.albums import OVERRIDES_FILE  # noqa: PLC0415
    from music_fetch.spotdl_ops import PINS_FILE  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="music-overrides",
        description="Search overrides, holds and track pins in the music database (homelab#2484); "
                    "every write is a dry run unless --apply")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="every override, hold and pin")
    p.add_argument("--kind", choices=ov.KINDS)

    def write(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
        return p

    p = write("search", "set an album's search override")
    p.add_argument("--reason", default="", help="why, kept with it")
    p.add_argument("album", help="album key, Spotify album ID or URL, release group, or 'artist — album'")
    p.add_argument("words", nargs="+", help="the search words (normalised as the tick does)")
    p = write("clear-search", "clear an album's search override")
    p.add_argument("album")
    p = write("hold", "keep an album out of the album tick's automatic reimports")
    p.add_argument("--reason", required=True, help="why, kept with it")
    p.add_argument("album")
    p = write("release", "release an album's hold")
    p.add_argument("album")
    p = write("pin", "download a track from a source URL instead of spotdl's search")
    p.add_argument("--label", default="")
    p.add_argument("--reason", default="")
    p.add_argument("track", help="Spotify track ID or open.spotify.com track URL")
    p.add_argument("url", help="a YouTube or YouTube Music video URL (other sites are refused)")
    p = write("unpin", "remove a track's pin")
    p.add_argument("track")
    p = write("import", "import album-overrides.conf and track-pins.conf (once; entries held already are skipped)")
    p.add_argument("--albums", type=Path, default=OVERRIDES_FILE)
    p.add_argument("--pins", type=Path, default=PINS_FILE)
    return parser


def _track(ref: str) -> str:
    try:
        track = spotify_key(ref.strip(), "track")
    except ValueError as exc:
        raise ov.Refused(str(exc)) from exc
    if not _TRACK_ID.fullmatch(track):
        raise ov.Refused(f"{ref!r} is not a Spotify track ID or URL")
    return track


def _listed(entry: dict) -> str:
    """One ``list`` line."""
    if entry["kind"] == ov.PIN:
        what = f"{entry['track'] or 'on no playlist store'} [{entry['spotify_track_id']}] → {entry['url']}"
    else:
        album = entry["album"]
        what = (f"detached ({', '.join(entry['refs'])})" if album is None
                else f"{album['artist']} — {album['name']} [{album['key']}]")
        if entry["kind"] == ov.SEARCH:
            what += f' "{entry["words"]}"'
    why = f" — {entry['reason']}" if entry["reason"] else ""
    return f"{entry['kind']}: {what} (set by {entry['set_by']} at {entry['set_at']}){why}"


def run(args: argparse.Namespace) -> None:
    from music_fetch.usenet import normalise  # noqa: PLC0415

    with _connect(write=args.command not in ("list", "import")) as conn:
        rows = statedb.load_albums(conn)
        if args.command == "list":
            for entry in ov.listing(conn, rows, args.kind):
                logger.info("%s", _listed(entry))
            return
        if args.command == "import":
            out_albums, out_pins, report = import_plan(conn, rows, args.albums, args.pins)
            for line in report:
                logger.info("%s", line)
            logger.info("Import: %d override(s) and hold(s), %d pin(s)", len(out_albums), len(out_pins))
            if args.apply:
                import_write(conn, out_albums, out_pins)
                logger.info("Imported: the tick and the nightly now read the music database's overrides; "
                            "the ConfigMap's files are ignored")
            else:
                logger.info("Dry run; --apply to write")
            return
        if args.command in ("search", "clear-search"):
            words = None
            if args.command == "search":
                words = normalise(" ".join(args.words))
                if not words:
                    raise ov.Refused("the search words normalise to nothing")
            key, record, lines = search_dry_run(conn, rows, args.album, words,
                                                args.reason if words else "")
            for line in lines:
                logger.info("%s", line)
            if args.apply:
                if words is None:
                    ov.clear_album(conn, ov.SEARCH, key, record)
                else:
                    ov.set_album(conn, ov.SEARCH, key, record, words=words, reason=args.reason)
        elif args.command in ("hold", "release"):
            hold = args.command == "hold"
            key, record, lines, had = hold_dry_run(conn, rows, args.album, hold, args.reason if hold else "")
            for line in lines:
                logger.info("%s", line)
            if args.apply:
                if hold:
                    ov.set_album(conn, ov.HOLD, key, record, reason=args.reason)
                elif had:
                    ov.clear_album(conn, ov.HOLD, key, record)
        else:
            track = _track(args.track)
            url = None
            if args.command == "pin":
                try:
                    url = ov.pin_source(args.url)
                except ValueError as exc:
                    raise ov.Refused(str(exc)) from exc
            pin, title, lines, had = pin_dry_run(conn, track, url, args.label if url else "",
                                                 args.reason if url else "")
            for line in lines:
                logger.info("%s", line)
            if args.apply:
                if pin is not None:
                    ov.set_pin(conn, pin, title)
                elif had:
                    ov.remove_pin(conn, track, title)
        logger.info("Applied" if args.apply else "Dry run; --apply to write")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        run(args)
    except (ov.Refused, statedb.Unavailable) as exc:
        logger.error("%s", exc)
        sys.exit(1)

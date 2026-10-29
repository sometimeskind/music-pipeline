"""Keep beets' ``album_record`` stamps on their album record's key (homelab#2478).

The album import stamps each item of a release with its record's key
(:func:`music_scan.release.claim`), and canon, dupes, supersede, the release
check and the rollback find the record by it.  Since ``music-album-rekey
--apply`` an album's key is its row id in the music database
(:data:`music_fetch.state.ALBUM_KEYS`), which survives the album resolving to a
release group.  A key still changes in two ways, and the stamps follow:

- **The one-time move** from the old key (the primary Spotify album ID, else
  the release group, else the name) to the row id, and back with ``--revert``:
  ``music-album-rekey``, an operator step (dry run unless ``--apply``, which
  backs ``library.db`` up first).
- **A row that goes**: two editions merged (the loser's row is deleted) or an
  album that left its playlists and came back (a new row).  The gone row's refs
  are in ``album_rows_gone``, so its items move to the record that holds one of
  them now.  Every scan does this before its canon, once the keys are row ids.

What a stamp may name (:func:`targets`): a record's key, its old-scheme key,
its row id, any of its Spotify album IDs or names, or a gone row whose refs it
holds.  A value two records claim moves nowhere, and a stamp that names no
record is left as it is (a record deleted for good: canon treats its items as
loose, as before).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from music_fetch import state as statedb

logger = logging.getLogger(__name__)

FIELD = "album_record"


@dataclasses.dataclass
class Move:
    item: object
    old: str
    new: str


def targets(records: Mapping[str, dict], gone: Mapping[str, list[str]] | None = None) -> dict[str, str]:
    """Every value an item's ``album_record`` may hold → the key of the record
    it names in *records* (keyed by the scheme the stamps move to).  *gone* is
    :func:`state.gone_rows`."""
    out: dict[str, str] = {}
    clash: set[str] = set()

    def add(alias: str | None, key: str) -> None:
        if not alias:
            return
        if out.setdefault(alias, key) != key:
            clash.add(alias)

    holder: dict[str, str] = {}
    for key, record in records.items():
        refs = [*(record.get("spotify_album_ids") or ()), *(record.get("names") or ()),
                *([record["release_group"]] if record.get("release_group") else [])]
        for alias in (key, statedb.album_key(record), *(record.get("spotify_album_ids") or ()),
                      *(record.get("names") or ())):
            add(alias, key)
        if record.get("_id") is not None:
            add(str(record["_id"]), key)
        for ref in refs:
            holder.setdefault(ref, key)
    for row, refs in (gone or {}).items():
        key = next((holder[r] for r in refs if r in holder), None)
        if key is not None:
            add(row, key)
    for alias in sorted(clash):
        logger.warning("[RESTAMP] %s names more than one album record; its items keep it", alias)
        del out[alias]
    return out


def plan(items: Iterable, records: Mapping[str, dict],
         gone: Mapping[str, list[str]] | None = None) -> tuple[list[Move], int]:
    """``(moves, orphans)``: the stamped *items* whose ``album_record`` is not
    their record's key in *records*, and the count naming no record."""
    to = targets(records, gone)
    moves: list[Move] = []
    orphans = 0
    for item in items:
        old = item.get(FIELD)
        if not old:
            continue
        new = to.get(old)
        if new is None:
            orphans += 1
        elif new != old:
            moves.append(Move(item, old, new))
    return moves, orphans


def report(moves: list[Move], records: Mapping[str, dict], orphans: int, verb: str = "restamped") -> None:
    """One ``[RESTAMP]`` line per album, then the summary."""
    by_album: dict[tuple[str, str], int] = {}
    for m in moves:
        by_album[(m.old, m.new)] = by_album.get((m.old, m.new), 0) + 1
    for (old, new), n in by_album.items():
        record = records.get(new) or {}
        logger.info("  [RESTAMP] %s — %s: %d item(s) %s → %s", record.get("artist", "?"), record.get("name", "?"),
                    n, old, new)
    if moves or orphans:
        logger.info("Restamp: %d item(s) on %d album(s) %s; %d stamped item(s) name no album record (left as they are)",
                    len(moves), len(set(by_album)), verb, orphans)


def apply(lib, moves: list[Move]) -> None:
    """Write *moves* to beets' database (a flexible attribute: no file is touched), in one transaction."""
    if not moves:
        return
    with lib._lib.transaction():
        for m in moves:
            m.item[FIELD] = m.new
            m.item.store()


def scan_step(lib) -> int:
    """Every scan, before canon: move the stamps of gone rows' items to the
    record holding their refs now.  Only once the keys are row ids; before the
    one-time move the stamps are the operator's to move.  Returns the count."""
    from music_fetch.albums import STATE_FILE, State  # noqa: PLC0415

    with statedb.session() as conn:
        if conn is None or not statedb.keyed_by_row(conn):
            return 0
        gone = statedb.gone_rows(conn)
    records = State.load(STATE_FILE).albums
    moves, orphans = plan(lib.all_items(), records, gone)
    if moves:
        apply(lib, moves)
        report(moves, records, orphans)
    return len(moves)


def backup(db_path: Path) -> Path:
    """A consistent copy of beets' database beside it, before ``--apply`` writes."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = db_path.with_name(f"{db_path.name}.pre-rekey-{stamp}")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="music-album-rekey",
        description="Move every album_record stamp to the album's row id, the album key from then on "
                    "(homelab#2478); dry run unless --apply")
    parser.add_argument("--apply", action="store_true",
                        help="back library.db up, switch the keys to row ids and restamp the items")
    parser.add_argument("--revert", action="store_true",
                        help="move the stamps back to the old keys (primary Spotify album ID) and switch back; "
                             "run it before rolling the image back to one from before homelab#2478")
    parser.add_argument("--verbose", action="store_true", help="list every album whose items move")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    from music_fetch.state_import import _locks  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    if not statedb.in_db():
        raise SystemExit("The state is not in the music database (music-state-import): nothing to rekey")
    by_row = not args.revert
    with _locks(args.apply), statedb.connect() as conn, MusicLibrary(LIBRARY_DB) as lib:
        current = statedb.keyed_by_row(conn)
        rows = statedb.load_albums(conn)
        rows.by_row = by_row
        rows.rekey()
        moves, orphans = plan(lib.all_items(), rows.albums, statedb.gone_rows(conn))
        target = "row ids" if by_row else "the old keys"
        logger.info("Album keys are %s now; this moves the stamps to %s", "row ids" if current else "the old keys",
                    target)
        if args.verbose or not args.apply:
            report(moves, rows.albums, orphans, "would be restamped" if not args.apply else "restamped")
        if not moves:
            logger.info("Every stamp is on %s already", target)
        if not args.apply:
            logger.info("Dry run; --apply backs library.db up, then writes")
            return
        copy = backup(LIBRARY_DB)
        logger.info("library.db backed up to %s", copy)
        if by_row:
            # The switch first: a restamp cut short is finished by the next scan (scan_step).
            statedb.put(conn, statedb.ALBUM_KEYS, {"by": "row", "at": datetime.now(timezone.utc).isoformat(
                timespec="seconds")})
            apply(lib, moves)
        else:
            # The stamps first: until the switch goes, a scan would move them on to row ids again.
            apply(lib, moves)
            statedb.delete(conn, statedb.ALBUM_KEYS)
        if not args.verbose:
            report(moves, rows.albums, orphans)
    logger.info("Album keys are %s; %d item(s) restamped", target, len(moves))

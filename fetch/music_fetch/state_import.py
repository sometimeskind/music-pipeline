"""The one-shot move of the JSON state into the music database (homelab#2410).

``music-state-import`` reads every JSON dot file :mod:`music_fetch.state`
replaces, rekeys the album store by release group (:mod:`music_fetch.resolve`)
and reports what it would write: albums rekeyed, flagged and merged, and the
stores, backoff entries and kv documents.  A dry run unless ``--apply``.

``--apply`` holds the ``albums`` and ``pipeline`` locks, reads the files again
and writes everything in one transaction with the :data:`state.IMPORTED`
marker, which switches the pipeline to the database.  It refuses a database
that has state already, unless ``--replace`` (which empties the state tables
first, never the events).  The JSON files are left as they are: the rollback
is the previous image, which reads them.  Back them up before ``--apply``.

``music-album-resolve`` reruns the resolve pass against the database once the
MusicBrainz cache or the library knows more.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from music_fetch import events, resolve, state

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
LIBRARY_DB = Path("/root/.config/beets/library.db")
# kv document → its dot file in SPOTDL_DIR, and whether the file maps keys to entries.
KV_FILES = {
    state.MB_RELEASES: (".mb-releases.json", True),
    state.CANON_ISRC: (".canon-isrc.json", True),
    state.CANON_PAGES: (".canon-pages.json", False),
    state.SPOTIFY_LIMIT: (".spotify-rate-limit.json", False),
    state.EMPTY_SLOTS: (".empty-slots.json", False),
}
STATE_TABLES = ("albums", "album_refs", "album_playlists", "indexer_use", "playlist_stores", "spotdl_failures", "kv")


def _read(path: Path) -> Any:
    """*path*'s JSON, or None when it is absent."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def to_rows(data: dict) -> state.AlbumRows:
    """``.albums.json``'s content as unresolved album rows, each keyed by its Spotify album ID (or name) for now."""
    albums: dict[str, dict] = {}
    for key, record in (data.get("albums") or {}).items():
        spotify = events.spotify_album(key)
        albums[key] = {**record, "release_group": None, "unresolved": state.PENDING,
                       "spotify_album_ids": [spotify] if spotify else [], "names": [] if spotify else [key]}
    return state.AlbumRows(playlists=data.get("playlists") or {}, albums=albums,
                           indexer=data.get("indexer") or [], mixed=list(data.get("mixed") or []))


@dataclasses.dataclass
class Plan:
    rows: state.AlbumRows
    report: resolve.Report
    stores: dict[str, dict]
    failures: dict[str, dict]
    kv: dict[str, dict[str, Any]]  # name → key → value ("" for a one-document name)


def plan(spotdl_dir: Path = SPOTDL_DIR, library_db: Path = LIBRARY_DB,
         overrides: dict[str, str] | None = None) -> Plan:
    """Everything the import would write, read from *spotdl_dir*'s dot files."""
    rows = to_rows(_read(spotdl_dir / ".albums.json") or {})
    kv: dict[str, dict[str, Any]] = {}
    for name, (filename, keyed) in KV_FILES.items():
        value = _read(spotdl_dir / filename)
        if value is not None:
            kv[name] = value if keyed else {"": value}
    stores = {}
    for path in sorted((spotdl_dir / ".store").glob("*.json")):
        data = _read(path) or {}
        stores[data.get("playlist") or path.stem] = data
    failures = _read(spotdl_dir.parent / ".spotdl-failures.json") or {}
    if library_db.is_file():
        library = resolve.library_groups(library_db)
    else:
        logger.warning("No beets library at %s: no library evidence", library_db)
        library = {}
    report = resolve.resolve(rows, kv.get(state.MB_RELEASES, {}), library, overrides)
    return Plan(rows, report, stores, failures, kv)


def _updated(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
    except ValueError:
        return None


def write(conn, p: Plan, replace: bool = False) -> None:
    """Write *p* and the import marker in one transaction."""
    with conn.transaction():
        held = conn.execute("SELECT (SELECT count(*) FROM albums) + (SELECT count(*) FROM playlist_stores) "
                            "+ (SELECT count(*) FROM kv)").fetchone()[0]
        if held and not replace:
            raise SystemExit("The music database has state already; --replace empties it first")
        if replace:
            conn.execute(f"TRUNCATE {', '.join(STATE_TABLES)} RESTART IDENTITY")
        state.save_albums(conn, p.rows)
        for name, data in p.stores.items():
            state.save_store(conn, name, data.get("entries") or [], data.get("source") or "",
                             _updated(data.get("updated") or ""))
        state.save_failures(conn, p.failures)
        for name, values in p.kv.items():
            state.put_many(conn, name, values)
        state.put(conn, state.IMPORTED, {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "summary": p.report.summary()})


def _counts(p: Plan) -> str:
    kv = ", ".join(f"{name} {len(values)}" for name, values in sorted(p.kv.items()))
    return (f"{len(p.rows.albums)} album row(s), {len(p.stores)} playlist store(s), "
            f"{len(p.failures)} backoff entr(ies), kv: {kv or 'none'}")


def _locks(apply: bool):
    """The ``albums`` and ``pipeline`` slots, so no tick, scan or nightly writes the JSON mid-import."""
    if not apply:
        return contextlib.nullcontext()
    from music_fetch.albums import _state_lock  # noqa: PLC0415
    from music_scan.audit import pipeline_lock  # noqa: PLC0415 — the image holds both packages

    stack = contextlib.ExitStack()
    stack.enter_context(_state_lock(True))
    stack.enter_context(pipeline_lock())
    return stack


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")


def import_main() -> None:
    parser = argparse.ArgumentParser(
        prog="music-state-import",
        description="Move the JSON state into the music database, rekeyed by release group (homelab#2410); "
                    "dry run unless --apply")
    parser.add_argument("--apply", action="store_true", help="write the database (back the JSON up first)")
    parser.add_argument("--replace", action="store_true", help="empty the state tables first (never the events)")
    parser.add_argument("--verbose", action="store_true", help="list every rekeyed and flagged album")
    args = parser.parse_args()
    _logging()
    from music_fetch.albums import load_overrides  # noqa: PLC0415

    with _locks(args.apply):
        p = plan(overrides=load_overrides())
        p.report.log(verbose=args.verbose)
        logger.info("Import: %s", _counts(p))
        if not args.apply:
            logger.info("Dry run; back the JSON up, then --apply")
            return
        with state.connect() as conn:
            write(conn, p, replace=args.replace)
    logger.info("Imported; the pipeline now reads the music database, and the JSON files are the rollback copy")


def resolve_main() -> None:
    parser = argparse.ArgumentParser(
        prog="music-album-resolve",
        description="Resolve and merge the album store's unkeyed albums again (homelab#2410); dry run unless --apply")
    parser.add_argument("--apply", action="store_true", help="write the database")
    parser.add_argument("--verbose", action="store_true", help="list every rekeyed and flagged album")
    args = parser.parse_args()
    _logging()
    from music_fetch.albums import _state_lock, load_overrides  # noqa: PLC0415

    with _state_lock(args.apply), state.connect() as conn:
        if state.get(conn, state.IMPORTED) is None:
            raise SystemExit("The state is not imported yet: run music-state-import")
        rows = state.load_albums(conn)
        library = resolve.library_groups(LIBRARY_DB) if LIBRARY_DB.is_file() else {}
        report = resolve.resolve(rows, state.get_all(conn, state.MB_RELEASES), library, load_overrides())
        report.log(verbose=args.verbose)
        if args.apply:
            logger.info("Saved %d row(s)", state.save_albums(conn, rows))
        else:
            logger.info("Dry run; --apply to write")

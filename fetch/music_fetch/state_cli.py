"""``music-state``: hand edits of the pipeline's state (homelab#2503, :mod:`music_fetch.state`).

The runbooks' edits, one command each, instead of SQL typed into an exec:

- ``backoff`` lists the spotdl backoff; ``clear-backoff`` empties it, so the
  next nightly retries every backed-off track (after new YouTube cookies);
- ``stores`` lists the playlist stores; ``drop-store -- <playlist>`` deletes
  one, so it is rebuilt from Spotify;
- ``clear-snapshot -- <playlist>`` forgets an album playlist's Spotify
  snapshot, so the next album tick re-reads the playlist and writes its store.

Every write is a dry run unless ``--apply``, and takes the locks of the flows
that write the same rows, so it waits for a run that holds them rather than
have that run write its copy back over the edit: the backoff the nightly's
(``pipeline``), a store the nightly's and the album tick's (``albums``, then
``pipeline``), a snapshot the tick's (``albums``, and the album store's save
lock in the transaction).  Each write records a ``state_edited`` event in its
transaction.  Run it between runs: a lock held by a long nightly can outlast
the wait (``pipeline``: 30 minutes, then it fails).

Once the state is in the music database (:func:`state.in_db`).  Before that
only ``clear-backoff`` works, by deleting the JSON file (a local compose run).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import psycopg

from music_fetch import events
from music_fetch import state as statedb
from music_fetch.store import LOCAL

logger = logging.getLogger(__name__)

SOURCE = "state"
# The backoff's file before the switch; ingest.FAILURES_FILE, without importing spotdl.
FAILURES_FILE = Path("/root/Music/inbox/.spotdl-failures.json")


class Refused(Exception):
    """The edit can't be made as asked; nothing was written."""


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _locks(names: tuple[str, ...], apply: bool) -> Iterator[None]:
    """The flows' slots *names*, in order (``albums`` before ``pipeline``, as
    the Music MCP's jobs take them); none for a dry run.  Each helper skips its
    lock when ``MUSIC_LOCKS_HELD`` names it or PREFECT_API_URL is unset.  A
    lock that times out releases the ones taken before it."""
    with contextlib.ExitStack() as stack:
        if apply and "albums" in names:
            from music_fetch.albums import _state_lock  # noqa: PLC0415

            stack.enter_context(_state_lock(True))
        if apply and "pipeline" in names:
            from music_scan.audit import pipeline_lock  # noqa: PLC0415 — the image holds both packages

            stack.enter_context(pipeline_lock())
        yield


def _connect() -> psycopg.Connection:
    if not statedb.in_db():
        raise Refused("the state is not in the music database yet (music-state-import)")
    return statedb.connect()


# ---------------------------------------------------------------------------
# The backoff
# ---------------------------------------------------------------------------


def _backoff_summary(failures: dict[str, dict]) -> str:
    if not failures:
        return "The backoff is empty"
    kinds = Counter(e.get("kind", "miss") for e in failures.values())
    retry = min((e.get("retry_after") or "")[:10] for e in failures.values())
    return (f"The backoff holds {len(failures)} track(s) "
            f"({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))}), next retry {retry}")


def list_backoff(conn: psycopg.Connection) -> list[str]:
    failures = statedb.load_failures(conn)
    lines = [f"{url}: {e.get('kind', 'miss')}, attempt {e.get('attempts', 0)}, until {(e.get('retry_after') or '')[:10]}"
             f"; {e.get('reason', '')}" for url, e in failures.items()]
    return [*lines, _backoff_summary(failures)]


def clear_backoff(conn: psycopg.Connection, apply: bool) -> list[str]:
    """Empty ``spotdl_failures``; the event carries the counts, not the tracks."""
    with conn.transaction():
        failures = {url: e for url, e in conn.execute(
            "SELECT url, entry FROM spotdl_failures ORDER BY url FOR UPDATE")}
        lines = [_backoff_summary(failures)]
        if not failures:
            return [*lines, "Nothing to clear"]
        lines.append("Clear: the next nightly (03:00 UTC) tries every one of them again")
        if apply:
            conn.execute("DELETE FROM spotdl_failures")
            kinds = Counter(e.get("kind", "miss") for e in failures.values())
            events.insert(conn, "state_edited", f"[STATE] backoff cleared: {len(failures)} track(s)",
                          source=SOURCE, data={"what": "backoff", "tracks": len(failures), "kinds": dict(kinds)})
    return lines


def clear_backoff_file(path: Path, apply: bool) -> list[str]:
    """Before the switch: the backoff is *path*; clearing it deletes it."""
    if not path.exists():
        return [f"No backoff file at {path}", "Nothing to clear"]
    lines = [f"Clear: delete {path}; the next run tries every backed-off track again"]
    if apply:
        path.unlink(missing_ok=True)
    return lines


# ---------------------------------------------------------------------------
# Playlist stores
# ---------------------------------------------------------------------------


def _counts(entries: list) -> tuple[int, int]:
    """(Spotify entries, local entries)."""
    local = sum(1 for e in entries if isinstance(e, dict) and e.get(LOCAL))
    return len(entries) - local, local


def list_stores(conn: psycopg.Connection) -> list[str]:
    album = {name for (name,) in conn.execute("SELECT playlist FROM album_playlists")}
    lines = []
    for name, data in statedb.load_stores(conn).items():
        spotify, local = _counts(data["entries"])
        lines.append(f"{name}: {spotify} Spotify + {local} local entr(ies), source {data['source'] or '-'}, "
                     f"updated {data['updated']}" + (" (album playlist)" if name in album else ""))
    lines.append(f"{len(lines)} playlist store(s)")
    return lines


def drop_store(conn: psycopg.Connection, playlist: str, apply: bool) -> list[str]:
    with conn.transaction():
        row = conn.execute("SELECT source, updated_at, entries FROM playlist_stores WHERE playlist = %s FOR UPDATE",
                           (playlist,)).fetchone()
        if row is None:
            raise Refused(f"no playlist store is named {playlist!r}; `music-state stores` lists them")
        source, updated, entries = row
        spotify, local = _counts(entries)
        album = conn.execute("SELECT 1 FROM album_playlists WHERE playlist = %s", (playlist,)).fetchone() is not None
        lines = [f"Store {playlist}: {spotify} Spotify + {local} local entr(ies), source {source or '-'}, "
                 f"updated {statedb._iso(updated)}",
                 "Drop: its .m3u stays as it was until the store is rebuilt"]
        if local:
            lines.append(f"Lost: its {local} local entr(ies); a rebuild from Spotify doesn't bring them back")
        lines.append("Rebuilt: when its Spotify snapshot changes; `music-state clear-snapshot` makes the next "
                     "album tick re-read it" if album else
                     "Rebuilt: by the next nightly (03:00 UTC), from Spotify")
        if apply:
            conn.execute("DELETE FROM playlist_stores WHERE playlist = %s", (playlist,))
            events.insert(conn, "state_edited", f"[STATE] store {playlist} dropped: {spotify} Spotify + {local} local "
                          "entr(ies)", source=SOURCE,
                          data={"what": "store", "playlist": playlist, "source": source,
                                "updated": statedb._iso(updated), "entries": spotify, "local": local})
    return lines


# ---------------------------------------------------------------------------
# Album playlist snapshots
# ---------------------------------------------------------------------------


def clear_snapshot(conn: psycopg.Connection, playlist: str, apply: bool) -> list[str]:
    with conn.transaction():
        # The album store's save lock: a save rewrites every album_playlists row.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (statedb._SAVE_LOCK,))
        row = conn.execute("SELECT data FROM album_playlists WHERE playlist = %s", (playlist,)).fetchone()
        if row is None:
            names = [n for (n,) in conn.execute("SELECT playlist FROM album_playlists ORDER BY playlist")]
            raise Refused(f"{playlist!r} is not an album playlist; the album playlists are: {', '.join(names) or 'none'}")
        snapshot = row[0].get("snapshot_id")
        lines = [f"Album playlist {playlist}: snapshot {snapshot or 'none'}"]
        if not snapshot:
            return [*lines, "Nothing to clear: the next album tick re-reads it already"]
        lines.append("Clear: the next album tick (every 30 minutes) re-reads the playlist from Spotify and writes "
                     "its store")
        if apply:
            conn.execute("UPDATE album_playlists SET data = data - 'snapshot_id' WHERE playlist = %s", (playlist,))
            events.insert(conn, "state_edited", f"[STATE] album playlist {playlist}: snapshot cleared",
                          source=SOURCE, data={"what": "snapshot", "playlist": playlist, "snapshot_id": snapshot})
    return lines


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


# The locks each write takes; see the module docstring.
LOCKS = {"clear-backoff": ("pipeline",), "drop-store": ("albums", "pipeline"), "clear-snapshot": ("albums",)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-state",
        description="Hand edits of the pipeline's state (homelab#2503); every write is a dry run unless --apply")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backoff", help="every backed-off track, with its kind, attempt and retry date")
    p = sub.add_parser("clear-backoff", help="empty the spotdl backoff: the next nightly retries every track")
    p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
    sub.add_parser("stores", help="every playlist store, with its entry counts, source and date")
    for name, what in (("drop-store", "delete a playlist store, so it is rebuilt from Spotify"),
                       ("clear-snapshot", "forget an album playlist's snapshot, so the next tick re-reads it")):
        p = sub.add_parser(name, help=what)
        p.add_argument("--apply", action="store_true", help="write it (a dry run without)")
        p.add_argument("playlist", help="the playlist's name, as in playlists.conf")
    return parser


def run(args: argparse.Namespace) -> None:
    apply = getattr(args, "apply", False)
    if args.command == "clear-backoff" and not statedb.in_db():
        lines = clear_backoff_file(FAILURES_FILE, apply)
    else:
        with _locks(LOCKS.get(args.command, ()), apply), _connect() as conn:
            if args.command == "backoff":
                lines = list_backoff(conn)
            elif args.command == "stores":
                lines = list_stores(conn)
            elif args.command == "clear-backoff":
                lines = clear_backoff(conn, apply)
            elif args.command == "drop-store":
                lines = drop_store(conn, args.playlist, apply)
            else:
                lines = clear_snapshot(conn, args.playlist, apply)
    for line in lines:
        logger.info("%s", line)
    if args.command in LOCKS and not lines[-1].startswith("Nothing"):
        logger.info("Applied" if apply else "Dry run; --apply to write")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        run(args)
    except (Refused, statedb.Unavailable) as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except TimeoutError:
        logger.error("A run held the lock too long (a nightly or scan); nothing was written: try again between runs")
        sys.exit(1)

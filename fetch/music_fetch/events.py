"""The event store: library history as rows, not log lines (homelab#2413).

Each point that logs a tag the nightly digest reads ([DONE], [RETIRE], [GRAB]…)
also calls :func:`record`, and the Music MCP's ``album_history`` and
``digest_summary`` read the rows back.  The log lines stay, for people; nothing
parses them any more.

Writes are best-effort.  The beets change and its event are not one
transaction, and an import never waits on the database: :func:`record` returns
False instead of raising, logs ``[EVENT] not recorded``, and stamps
``music_events_write_failure_timestamp_seconds`` on the Pushgateway, which the
homelab alert watches.  A failed connection is not retried for
:data:`RETRY_SECONDS`, so a database that is down costs an import one connect
timeout a minute, not one per event.  With ``MUSIC_DATABASE_URL`` unset nothing
is recorded and nothing counts as a failure (tests, a local run).

A process (the service, a CLI the MCP queue runs, the beets plugin) keeps one
connection.  ``MUSIC_EVENT_CALLER`` and ``MUSIC_EVENT_JOB`` in its environment
attribute its events to the MCP caller and job that started it.

Retention (:func:`prune`): events older than :data:`KEEP_DAYS` go, except each
album's latest :data:`KEEP_PER_ALBUM`, so a quiet album keeps its history.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from music_fetch import db, metrics

logger = logging.getLogger(__name__)

KINDS = frozenset(db.EVENT_KINDS)
RETRY_SECONDS = 60
# The failure stamp is pushed at most this often, so a database outage doesn't
# add a Pushgateway round trip to every event.
PUSH_SECONDS = 60
KEEP_DAYS = 90
KEEP_PER_ALBUM = 20
FAILURE_METRIC = "music_events_write_failure_timestamp_seconds"

_INSERT = """
INSERT INTO events (kind, source, spotify_album_id, release_group, item_id, caller, job_id, message, data)
VALUES (%(kind)s, %(source)s, %(spotify_album_id)s, %(release_group)s, %(item_id)s, %(caller)s, %(job_id)s,
        %(message)s, %(data)s)
"""

# An album's key for retention: its release group once known, else its Spotify ID.
_ALBUM_KEY = "coalesce(release_group::text, spotify_album_id)"
_PRUNE = f"""
DELETE FROM events
 WHERE at < now() - make_interval(days => %(days)s)
   AND id NOT IN (
       SELECT id FROM (
           SELECT id, row_number() OVER (PARTITION BY {_ALBUM_KEY} ORDER BY at DESC, id DESC) AS n
             FROM events
            WHERE {_ALBUM_KEY} IS NOT NULL) ranked
        WHERE n <= %(keep)s)
"""

_lock = threading.Lock()
_conn: psycopg.Connection | None = None
_retry_at = 0.0
_pushed_at = float("-inf")
_disabled_logged = False
# Failed writes in this process, for tests and the service's own logs.
failures = 0


class _Unavailable(Exception):
    """The database failed within the last RETRY_SECONDS; not tried again yet."""


def release_group_id(value: Any) -> str | None:
    """*value* as a release-group MBID, or None (beets stores a missing one as "")."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        logger.warning("[EVENT] %r is not a release-group MBID; recorded without one", value)
        return None


def record(kind: str, message: str, *, source: str, spotify_album_id: str | None = None,
           release_group: str | None = None, item_id: int | None = None,
           data: dict[str, Any] | None = None) -> bool:
    """Record one event; True when it was written.  Never raises.

    *source* is the component that saw it (``albums``, ``scan``, ``canon``,
    ``dupes``, ``mcp``); *message* is the human line, usually the log line's
    text; *data* holds whatever else the event carries (reason, paths, counts).
    """
    global failures
    if not db.configured():
        _log_disabled()
        return False
    if kind not in KINDS:
        # A programming error, but history is never worth an exception in an import.
        logger.error("[EVENT] unknown kind %r: %s", kind, message)
        failures += 1
        _stamp_failure()
        return False
    row = {
        "kind": kind,
        "source": source,
        "spotify_album_id": spotify_album_id or None,
        "release_group": release_group_id(release_group),
        "item_id": item_id,
        "caller": os.environ.get("MUSIC_EVENT_CALLER") or None,
        "job_id": os.environ.get("MUSIC_EVENT_JOB") or None,
        "message": message,
        "data": Jsonb(data or {}),
    }
    try:
        with _lock:
            _connection().execute(_INSERT, row)
        return True
    except Exception as exc:
        failures += 1
        logger.warning("[EVENT] not recorded (%s): %s %s", _reason(exc), kind, message)
        _stamp_failure()
        return False


def prune(conn: psycopg.Connection | None = None, *, days: int = KEEP_DAYS, keep: int = KEEP_PER_ALBUM) -> int:
    """Delete events past retention; returns how many.  Raises on a database error."""
    if conn is not None:
        return conn.execute(_PRUNE, {"days": days, "keep": keep}).rowcount
    with _lock:
        return _connection().execute(_PRUNE, {"days": days, "keep": keep}).rowcount


def _connection() -> psycopg.Connection:
    """This process's connection, opened on first use.  Call with _lock held."""
    global _conn, _retry_at
    if _conn is not None and not _conn.closed and not _conn.broken:
        return _conn
    if time.monotonic() < _retry_at:
        raise _Unavailable
    try:
        _conn = db.connect()
    except Exception:
        _conn = None
        _retry_at = time.monotonic() + RETRY_SECONDS
        raise
    return _conn


def _reason(exc: Exception) -> str:
    if isinstance(exc, _Unavailable):
        return f"database unavailable, retrying within {RETRY_SECONDS}s"
    return f"{type(exc).__name__}: {str(exc).strip() or 'no detail'}"


def _stamp_failure() -> None:
    global _pushed_at
    now = time.monotonic()
    if now - _pushed_at < PUSH_SECONDS:
        return
    _pushed_at = now
    metrics._push(metrics._gauge(FAILURE_METRIC, int(time.time())), "music_events")


def _log_disabled() -> None:
    global _disabled_logged
    if not _disabled_logged:
        _disabled_logged = True
        logger.info("[EVENT] %s is unset; events are not recorded", db.ENV)


def _reset() -> None:
    """Forget the connection and the backoff (tests)."""
    global _conn, _retry_at, _pushed_at, _disabled_logged, failures
    if _conn is not None:
        _conn.close()
    _conn, _retry_at, _pushed_at, _disabled_logged, failures = None, 0.0, float("-inf"), False, 0

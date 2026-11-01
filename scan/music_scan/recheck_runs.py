"""A recorded ``--recheck`` run's state in the music database (homelab#2553).

A whole-library recheck takes about an hour at AcoustID's 3 lookups a second,
so it is recorded by a run ID: a ``long_runs`` row holds its checkpoint (how
many items it has walked, the last item ID, the counts so far) and
``recheck_findings`` the items it flagged.  A run started again with its ID
continues after its checkpoint instead of starting over, and its apply
(``music-mb-fingerprint --replace-run``) works from the findings, never from a
re-run of the lookups.  Migration 12 creates both tables.

A run is held by one process at a time: :func:`hold` takes a session advisory
lock on the run ID, which Postgres releases when the connection goes away (a
killed process, a restarted pod).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

TOOL = "recheck"
# Never a flag: it starts with a letter or digit.
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# A finished run's findings are kept this long (the nightly prunes them) ...
KEEP_DAYS = 30
# ... and applied only this long after it finished: older findings describe a library that has moved on.
APPLY_WITHIN_DAYS = 7
# pg_advisory_lock(LOCK_CLASS, hashtext(run_id)): the two-key space, apart from db's migration lock.
LOCK_CLASS = 2553

RUNNING, DONE, FAILED = "running", "done", "failed"


class Refused(Exception):
    """The run can't be started, continued or applied; the message says why."""


def check_id(run_id: str) -> str:
    if not RUN_ID.match(run_id or ""):
        raise Refused(f"run ID {run_id!r}: 1 to 64 letters, digits, '-' or '_', starting with a letter or digit")
    return run_id


def hold(conn: psycopg.Connection, run_id: str) -> None:
    """Take the run's advisory lock for this connection's life; refuses when another process holds it."""
    (got,) = conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))", (LOCK_CLASS, run_id)).fetchone()
    if not got:
        raise Refused(f"run {run_id} is running in another process")


def get(conn: psycopg.Connection, run_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT run_id, tool, args, status, position, cursor, total, counts, caller, job_id, "
                       "created_at, updated_at, finished_at FROM long_runs WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c.name for c in cur.description], row))


def open_run(conn: psycopg.Connection, run_id: str, args: dict | None, caller: str | None,
             job_id: str | None) -> tuple[dict[str, Any], bool]:
    """The run *run_id*, created with *args* or continued; returns it and whether it continues.

    A run is continued with the arguments it was started with: *args* None
    takes them, other *args* are refused.  A finished run is never re-run
    (start a new run), and None for a run that doesn't exist starts nothing."""
    with conn.transaction():
        run = get(conn, run_id)
        if run is None and args is None:
            raise Refused(f"no run {run_id} to continue")
        if run is None:
            conn.execute("INSERT INTO long_runs (run_id, tool, args, status, caller, job_id) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (run_id, TOOL, Jsonb(args), RUNNING, caller, job_id))
            return get(conn, run_id), False  # type: ignore[return-value]
        if run["tool"] != TOOL:
            raise Refused(f"run {run_id} is a {run['tool']} run, not a recheck")
        if run["status"] == DONE:
            raise Refused(f"run {run_id} finished at {_iso(run['finished_at'])}; start a new run to check again")
        if args is not None and run["args"] != args:
            raise Refused(f"run {run_id} was started with {run['args']}, not {args}")
        conn.execute("UPDATE long_runs SET status = %s, job_id = coalesce(%s, job_id), updated_at = now() "
                     "WHERE run_id = %s", (RUNNING, job_id, run_id))
        run.update(status=RUNNING)
        return run, True


def checkpoint(conn: psycopg.Connection, run_id: str, position: int, cursor: int | None, total: int,
               counts: dict, findings: list) -> None:
    """Store how far the run has got and the findings since the last checkpoint, in one transaction."""
    with conn.transaction():
        for f in findings:
            path, size, mtime = f.file
            conn.execute(
                "INSERT INTO recheck_findings (run_id, item_id, guess, replacement, found, keeper, "
                "file_path, file_size, file_mtime_ns) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id, item_id) DO UPDATE SET guess = excluded.guess, "
                "replacement = excluded.replacement, found = excluded.found, keeper = excluded.keeper, "
                "file_path = excluded.file_path, file_size = excluded.file_size, "
                "file_mtime_ns = excluded.file_mtime_ns",
                (run_id, f.item_id, f.guess, f.replacement, Jsonb([list(p) for p in f.found]), f.keeper,
                 path, size, mtime))
        conn.execute("UPDATE long_runs SET position = %s, cursor = %s, total = %s, counts = %s, updated_at = now() "
                     "WHERE run_id = %s", (position, cursor, total, Jsonb(dict(counts)), run_id))


def finish(conn: psycopg.Connection, run_id: str, status: str) -> None:
    conn.execute("UPDATE long_runs SET status = %s, updated_at = now(), "
                 "finished_at = CASE WHEN %s = 'done' THEN now() END WHERE run_id = %s", (status, status, run_id))


def findings(conn: psycopg.Connection, run_id: str) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT item_id, guess, replacement, found, keeper, file_path, file_size, file_mtime_ns, "
                       "outcome FROM recheck_findings WHERE run_id = %s ORDER BY item_id", (run_id,))
    names = [c.name for c in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def for_apply(conn: psycopg.Connection, run_id: str, now: datetime | None = None) -> dict[str, Any]:
    """The run *run_id* when its findings may be applied; raises :class:`Refused` otherwise."""
    run = get(conn, run_id)
    if run is None:
        raise Refused(f"no run {run_id} (unknown, or pruned {KEEP_DAYS} days after it finished)")
    if run["tool"] != TOOL:
        raise Refused(f"run {run_id} is a {run['tool']} run, not a recheck")
    if run["status"] != DONE:
        raise Refused(f"run {run_id} is {run['status']}, not finished: continue it with --recheck --run-id "
                      f"{run_id}, then apply it")
    now = now or datetime.now(timezone.utc)
    if run["finished_at"] < now - timedelta(days=APPLY_WITHIN_DAYS):
        raise Refused(f"run {run_id} finished at {_iso(run['finished_at'])}, more than {APPLY_WITHIN_DAYS} days "
                      "ago; start a new run")
    return run


def record_outcome(conn: psycopg.Connection, run_id: str, item_id: int, outcome: str, reason: str | None,
                   job_id: str | None) -> None:
    """A finding's outcome; inside the caller's transaction when it has one."""
    conn.execute("UPDATE recheck_findings SET outcome = %s, outcome_reason = %s, outcome_at = now(), "
                 "outcome_job = %s WHERE run_id = %s AND item_id = %s", (outcome, reason, job_id, run_id, item_id))


def prune(conn: psycopg.Connection, days: int = KEEP_DAYS) -> int:
    """Delete runs (and their findings) last touched more than *days* ago; returns how many."""
    return conn.execute("DELETE FROM long_runs WHERE coalesce(finished_at, updated_at) < "
                        "now() - make_interval(days => %s)", (days,)).rowcount


def _iso(t: datetime | None) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else "?"

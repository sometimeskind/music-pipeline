"""The MCP job queue (homelab#2412): one CLI job at a time, records on pipeline-state.

Two lanes (homelab#2553): the short lane runs the tools one at a time from a
queue of :data:`DEPTH`; the long lane runs at most one long job (an hour-long
recheck) beside it, so the short tools keep working.  A long job takes no lock
of its own: one whose CLI writes takes its locks inside, for that phase only.

Each job runs one or more CLI commands as subprocesses, one after another.
A write tool's apply holds Prefect's locks for its whole run: it runs the dry
run again, compares that output's hash with the dry run the caller saw, and
runs the ``--apply`` commands only when they match.  The subprocess gets
``MUSIC_LOCKS_HELD``, so the CLI doesn't wait for a lock its job holds.

Records live in one JSON index, each job's output in its own log file next to
it; the last :data:`KEEP_JOBS` jobs and every job of the last
:data:`KEEP_DAYS` days are kept.  Jobs left queued or running by a restart are
marked ``interrupted`` when the queue loads, never retried, except a
*resumable* one (a long job, or a resumable tool's apply once its dry-run
comparison has passed): it is queued again
with the same job ID and its log appended, up to :data:`MAX_RESUMES` times.  Its
CLI continues from its own checkpoint (``--run-id``) or, for an apply, checks
each item again; a resumed apply runs its ``--apply`` commands under its locks
without the dry-run comparison, which passed before the restart.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from music_fetch import events

logger = logging.getLogger(__name__)
# The service's root logger may sit at WARNING under Prefect's logging config;
# the [MCP] lines must reach the pod log (and Loki) all the same.
logger.setLevel(logging.INFO)

JOBS_FILE = Path("/root/Music/inbox/spotdl/.mcp-jobs.json")
DEPTH = 5
KEEP_JOBS = 50
KEEP_DAYS = 7
DRY_RUN_WINDOW_SECONDS = 15 * 60
SHORT, LONG = "short", "long"
# A resumable job is resumed after this many restarts at most, then interrupted (a job that kills the pod).
MAX_RESUMES = 5
# In a command, replaced by the job's ID when it is submitted: a long job's run ID is its job ID.
JOB_ID = "{job_id}"

QUEUED = "queued"
WAITING = "waiting_for_lock"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REFUSED = "refused"
INTERRUPTED = "interrupted"
ACTIVE = (QUEUED, WAITING, RUNNING)

PAGE_LINES = 200
PAGE_BYTES = 32 * 1024

# "[2026-10-27T14:03:11+0000] ": the CLIs' logging prefix.
_LOG_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[^\]]*\]\s?")
_ONLY_TIMESTAMP = re.compile(r"^\s*\[?\d{4}-\d{2}-\d{2}[T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?\]?\s*$")
# "[2026-11-01T10:00:00+0000] [PROGRESS] 500/9210, 9 flagged so far"
_PROGRESS = re.compile(r"^(?:\[(?P<at>\d{4}-\d{2}-\d{2}T[^\]]*)\]\s?)?(?P<line>\[PROGRESS\] (?P<done>\d+)/(?P<total>\d+).*)$")


class Busy(Exception):
    """The queue is full; *queue* summarises the jobs in it."""

    def __init__(self, jobs: list[dict]) -> None:
        super().__init__("queue full")
        self.queue = jobs


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(t: str | None) -> datetime | None:
    return datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None


def output_hash(lines: list[str]) -> str:
    """The hash an apply compares: each line without its log timestamp, and no
    line that is only a timestamp."""
    kept = [_LOG_PREFIX.sub("", line) for line in lines]
    kept = [line for line in kept if not _ONLY_TIMESTAMP.match(line)]
    return "sha256:" + hashlib.sha256("\n".join(kept).encode()).hexdigest()


def progress(lines: list[str]) -> dict | None:
    """The job's latest ``[PROGRESS]`` line: how far it has got, and when."""
    for line in reversed(lines):
        if m := _PROGRESS.match(line):
            return {"done": int(m["done"]), "total": int(m["total"]), "line": m["line"], "at": m["at"]}
    return None


def same_args(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def page(lines: list[str], offset: int, finished: bool) -> tuple[str, int | None]:
    """At most :data:`PAGE_LINES` lines and :data:`PAGE_BYTES` from *offset*,
    and the offset that continues it (None once a finished job has no more)."""
    out: list[str] = []
    size = 0
    i = offset
    while i < len(lines) and len(out) < PAGE_LINES:
        line = lines[i]
        if len(line.encode()) > PAGE_BYTES:
            line = line.encode()[:PAGE_BYTES - 4].decode(errors="ignore") + "…"
        if out and size + len(line.encode()) + 1 > PAGE_BYTES:
            break
        out.append(line)
        size += len(line.encode()) + 1
        i += 1
    more = i < len(lines) or not finished
    return "\n".join(out), (i if more else None)


def run_subprocess(argv: list[str], env: dict[str, str], on_line: Callable[[str], None]) -> int:
    """Run *argv*, its stdout and stderr merged, a line at a time to *on_line*."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                          errors="replace", bufsize=1) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()


_albums_lock = threading.Lock()


@contextlib.contextmanager
def hold_locks(names: tuple[str, ...]) -> Iterator[None]:
    """Prefect's global limits *names*, in order; in direct mode (no
    PREFECT_API_URL) the service's threading locks instead."""
    with contextlib.ExitStack() as stack:
        for name in names:
            if os.environ.get("PREFECT_API_URL"):
                from prefect.concurrency.sync import concurrency  # noqa: PLC0415
                stack.enter_context(concurrency(name, occupy=1))
            else:
                from music_service import prefect_client  # noqa: PLC0415
                stack.enter_context(prefect_client._lock if name == "pipeline" else _albums_lock)
        yield


class JobQueue:
    """The single-slot queue and its records."""

    def __init__(self, path: Path = JOBS_FILE, depth: int = DEPTH,
                 run_command: Callable[[list[str], dict[str, str], Callable[[str], None]], int] = run_subprocess,
                 locks: Callable[[tuple[str, ...]], contextlib.AbstractContextManager] = hold_locks) -> None:
        self.path = path
        self.logs = path.with_suffix("")  # .mcp-jobs/ next to .mcp-jobs.json
        self.depth = depth
        self._run_command = run_command
        self._locks = locks
        self._mu = threading.RLock()
        self._pending: dict[str, queue.Queue[str]] = {SHORT: queue.Queue(), LONG: queue.Queue()}
        self._done: dict[str, threading.Event] = {}
        self._live: dict[str, list[str]] = {}  # output of the jobs running now
        self._specs: dict[str, dict] = {}  # what a queued job runs; not persisted
        self._workers: dict[str, threading.Thread] = {}
        resumed: list[dict] = []
        self.jobs: dict[str, dict] = self._load(resumed)
        for job in sorted(resumed, key=lambda j: j["created_at"]):
            self._enqueue(job, self._resume_spec(job))

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def _load(self, resumed: list[dict]) -> dict[str, dict]:
        """The records; a resumable job left active by a restart is queued again (appended to *resumed*)."""
        try:
            jobs = json.loads(self.path.read_text(encoding="utf-8")).get("jobs", {})
        except FileNotFoundError:
            return {}
        except Exception:
            logger.exception("[MCP] could not read %s; starting with no job records", self.path)
            return {}
        lost = [j for j in jobs.values() if j.get("status") in ACTIVE]
        for job in lost:
            resumes = job.get("resumes", 0)
            if job.get("resumable") and job.get("mode") == "apply" and not job.get("applying"):
                # Cut short before its dry-run comparison passed: nothing applied, and no guard to resume past.
                job.update(status=INTERRUPTED, finished_at=now(),
                           error="the service restarted before the apply began; nothing was applied")
                logger.info("[MCP] job=%s interrupted by a restart before its apply (tool=%s caller=%s)",
                            job["job_id"], job.get("tool"), job.get("caller"))
                _record_end(job)
                continue
            if job.get("resumable") and resumes < MAX_RESUMES:
                job.update(status=QUEUED, resumes=resumes + 1, resumed_at=now())
                logger.info("[MCP] job=%s resumed after a restart (%d of %d; tool=%s caller=%s)",
                            job["job_id"], resumes + 1, MAX_RESUMES, job.get("tool"), job.get("caller"))
                resumed.append(job)
                continue
            error = (f"the service restarted; resumed {resumes} times already, not again" if job.get("resumable")
                     else "the service restarted")
            job.update(status=INTERRUPTED, finished_at=now(), error=error)
            logger.info("[MCP] job=%s interrupted by a restart (tool=%s caller=%s)",
                        job["job_id"], job.get("tool"), job.get("caller"))
            _record_end(job)
        if lost:
            self._save(jobs)
        return jobs

    @staticmethod
    def _resume_spec(job: dict) -> dict:
        """What a resumed job runs: its commands again, an apply's ``--apply`` commands only."""
        if job.get("mode") == "apply":
            return {"commands": job["argv"], "apply_commands": None, "expected_hash": None, "resumed_apply": True}
        return {"commands": job["argv"], "apply_commands": None, "expected_hash": None}

    def _save(self, jobs: dict[str, dict] | None = None) -> None:
        jobs = self.jobs if jobs is None else jobs
        with self._mu:
            self._prune(jobs)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"jobs": jobs}, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def _prune(self, jobs: dict[str, dict]) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
        newest = sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
        for n, job in enumerate(newest):
            if n < KEEP_JOBS or job.get("status") in ACTIVE or (_parse(job["created_at"]) or cutoff) >= cutoff:
                continue
            del jobs[job["job_id"]]
            (self.logs / f"{job['job_id']}.log").unlink(missing_ok=True)

    def _update(self, job: dict, **fields) -> None:
        with self._mu:
            job.update(fields)
            self._save()

    def get(self, job_id: str) -> dict | None:
        with self._mu:
            job = self.jobs.get(job_id)
            return None if job is None else dict(job)

    def output(self, job_id: str) -> list[str]:
        with self._mu:
            if job_id in self._live:
                return list(self._live[job_id])
        try:
            return (self.logs / f"{job_id}.log").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def active(self, lane: str | None = None) -> list[dict]:
        with self._mu:
            jobs = [j for j in self.jobs.values()
                    if j.get("status") in ACTIVE and lane in (None, j.get("lane", SHORT))]
        return [summary(j) for j in sorted(jobs, key=lambda j: j["created_at"])]

    def find_dry_run(self, tool: str, args: dict, window: float = DRY_RUN_WINDOW_SECONDS) -> dict | None:
        """The latest dry run of *tool* with *args*, by any caller, that finished
        successfully within *window* seconds: a dry-run job, or an apply refused
        because its fresh dry run no longer matched."""
        since = datetime.now(timezone.utc) - timedelta(seconds=window)
        with self._mu:
            hits = [j for j in self.jobs.values()
                    if j.get("tool") == tool and j.get("dry_run_hash") and same_args(j.get("args", {}), args)
                    and (_parse(j.get("finished_at")) or since) > since]
        return max(hits, key=lambda j: (j["finished_at"], j.get("seq", 0)), default=None)

    # ------------------------------------------------------------------
    # Submitting and waiting
    # ------------------------------------------------------------------

    def submit(self, tool: str, args: dict, mode: str, caller: str, tier: str, commands: list[list[str]],
               apply_commands: list[list[str]] | None = None, expected_hash: str | None = None,
               locks: tuple[str, ...] = (), lane: str = SHORT, resumable: bool = False) -> dict:
        """Queue a job and return its record; raises :class:`Busy` when its lane is full
        (the short lane holds :attr:`depth` jobs, the long lane one)."""
        with self._mu:
            active = self.active(lane)
            if len(active) >= (self.depth if lane == SHORT else 1):
                raise Busy(active)
            job_id = uuid.uuid4().hex[:16]

            def fill(argvs: list[list[str]] | None) -> list[list[str]] | None:
                return None if argvs is None else [[a.replace(JOB_ID, job_id) for a in argv] for argv in argvs]

            commands, apply_commands = fill(commands), fill(apply_commands)  # type: ignore[assignment]
            job = {"job_id": job_id, "tool": tool, "args": args, "mode": mode, "status": QUEUED,
                   "caller": caller, "tier": tier, "argv": apply_commands if apply_commands else commands,
                   "locks": list(locks), "lane": lane, "resumable": resumable, "resumes": 0,
                   "created_at": now(), "started_at": None, "finished_at": None,
                   "exit_code": None, "dry_run_hash": None, "error": None}
            if apply_commands:
                job["dry_run_argv"] = commands
            self.jobs[job_id] = job
            self._save()
            self._enqueue(job, {"commands": commands, "apply_commands": apply_commands,
                                "expected_hash": expected_hash})
            return dict(job)

    def _enqueue(self, job: dict, spec: dict) -> None:
        lane = job.get("lane", SHORT)
        with self._mu:
            self._specs[job["job_id"]] = spec
            self._done[job["job_id"]] = threading.Event()
            self._pending[lane].put(job["job_id"])
            worker = self._workers.get(lane)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._work, args=(lane,), name=f"mcp-jobs-{lane}", daemon=True)
                self._workers[lane] = worker
                worker.start()

    def wait(self, job_id: str, timeout: float) -> dict | None:
        """The job's record once it finishes, or as it stands after *timeout* seconds."""
        event = self._done.get(job_id)
        if event is not None:
            event.wait(timeout)
        return self.get(job_id)

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def _work(self, lane: str) -> None:
        while True:
            job_id = self._pending[lane].get()
            try:
                self._run(job_id)
            except Exception as exc:  # noqa: BLE001 — record it and go on to the next job
                logger.exception("[MCP] job=%s crashed", job_id)
                job = self.jobs.get(job_id)
                if job is not None:
                    self._update(job, status=FAILED, finished_at=now(), error=f"{type(exc).__name__}: {exc}")
                    _record_end(job)
            finally:
                with self._mu:
                    self._live.pop(job_id, None)
                    self._specs.pop(job_id, None)
                event = self._done.pop(job_id, None)
                if event is not None:
                    event.set()

    def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]
        spec = self._specs[job_id]
        locks = tuple(job["locks"])
        logger.info("[MCP] job=%s start tool=%s mode=%s caller=%s tier=%s argv=%s", job_id, job["tool"],
                    job["mode"], job["caller"], job["tier"], json.dumps(job["argv"]))
        self.logs.mkdir(parents=True, exist_ok=True)
        log_path = self.logs / f"{job_id}.log"
        resumed = job.get("resumes", 0) > 0
        with self._mu:
            # A resumed job's output goes on after what it printed before the restart.
            self._live[job_id] = self.output(job_id) if resumed else []
        if locks:
            self._update(job, status=WAITING)
            logger.info("[MCP] job=%s waiting for lock(s) %s", job_id, ", ".join(locks))
        with self._locks(locks), log_path.open("a" if resumed else "w", encoding="utf-8") as log:
            self._update(job, status=RUNNING, started_at=now())
            # MUSIC_LOCKS_HELD is only ever set here, for the locks this job holds.
            env = {k: v for k, v in os.environ.items() if k != "MUSIC_LOCKS_HELD"}
            env["PYTHONUNBUFFERED"] = "1"
            # The events the CLI records name this job and its caller (homelab#2413).
            env["MUSIC_EVENT_CALLER"] = job["caller"]
            env["MUSIC_EVENT_JOB"] = job_id
            if locks:
                env["MUSIC_LOCKS_HELD"] = ",".join(locks)

            def emit(line: str) -> None:
                with self._mu:
                    self._live[job_id].append(line)
                log.write(line + "\n")
                log.flush()
                logger.info("[MCP] job=%s | %s", job_id, line)

            def run_all(commands: list[list[str]]) -> int:
                for argv in commands:
                    code = self._run_command(argv, env, emit)
                    if code != 0:
                        return code
                return 0

            if resumed:
                emit(f"[MCP] resumed after a restart ({job['resumes']} of {MAX_RESUMES})"
                     + ("; the dry run matched before it, so the apply goes on, checking each item again"
                        if spec.get("resumed_apply") else ""))
            code = run_all(spec["commands"])
            if spec.get("resumed_apply"):
                self._finish(job, DONE if code == 0 else FAILED, code)
                return
            if spec["apply_commands"] is None or code != 0:
                fields = {"dry_run_hash": output_hash(self._live[job_id])} if code == 0 else {}
                self._finish(job, DONE if code == 0 else FAILED, code, **fields)
                return
            fresh = output_hash(self._live[job_id])
            if fresh != spec["expected_hash"]:
                emit(f"[MCP] refused: this dry run no longer matches the one to apply ({spec['expected_hash']}); "
                     "nothing changed. It is now the dry run to apply.")
                self._finish(job, REFUSED, 0, dry_run_hash=fresh)
                return
            # The output is the apply's; the matching dry run is in the pod log.
            with self._mu:
                self._live[job_id] = []
            log.seek(0)
            log.truncate()
            emit(f"[MCP] dry run re-run matches ({fresh}); applying")
            self._update(job, applying=True)  # from here a restart resumes the apply (homelab#2553)
            code = run_all(spec["apply_commands"])
            self._finish(job, DONE if code == 0 else FAILED, code)

    def _finish(self, job: dict, status: str, code: int, **fields) -> None:
        # finished_at is to the second; seq orders jobs that finish within one.
        with self._mu:
            seq = max((j.get("seq", 0) for j in self.jobs.values()), default=0) + 1
        self._update(job, status=status, exit_code=code, finished_at=now(), seq=seq, **fields)
        logger.info("[MCP] job=%s end status=%s exit=%s tool=%s caller=%s", job["job_id"], status, code,
                    job["tool"], job["caller"])
        _record_end(job)


def _record_end(job: dict) -> None:
    """The job's ``mcp_job`` event (homelab#2413)."""
    events.record("mcp_job", f"[MCP] job={job['job_id']} end status={job.get('status')} "
                             f"exit={job.get('exit_code')} tool={job.get('tool')} caller={job.get('caller')}",
                  source="mcp", caller=job.get("caller"), job_id=job["job_id"],
                  data={k: job.get(k) for k in ("tool", "args", "mode", "tier", "status", "exit_code", "error",
                                                "created_at", "started_at", "finished_at")})


def summary(job: dict) -> dict:
    return {k: job.get(k) for k in ("job_id", "tool", "mode", "status", "caller", "created_at", "lane")}

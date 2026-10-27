"""Tests for music_service.mcp_jobs — the MCP job queue (homelab#2412)."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from music_service import mcp_jobs
from music_service.mcp_jobs import JobQueue, output_hash, page

_START_ENV = dict(os.environ)


class Commands:
    """A fake run_command: *outputs* maps an argv (joined) to (lines, exit code)."""

    def __init__(self, outputs: dict[str, tuple[list[str], int]] | None = None) -> None:
        self.outputs = outputs or {}
        self.ran: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, env, on_line):
        self.ran.append(argv)
        self.envs.append(env)
        lines, code = self.outputs.get(" ".join(argv), ([f"ran {' '.join(argv)}"], 0))
        for line in lines:
            on_line(line)
        return code


class Locks:
    def __init__(self, queue_ref: list) -> None:
        self.held: list[tuple[str, ...]] = []
        self.status_on_enter: list[str] = []
        self.queue_ref = queue_ref

    @contextlib.contextmanager
    def __call__(self, names):
        jobs = self.queue_ref[0].jobs
        self.status_on_enter.extend(j["status"] for j in jobs.values() if j["status"] == mcp_jobs.WAITING)
        self.held.append(names)
        yield


@pytest.fixture
def make(tmp_path):
    def make(outputs=None, **kw):
        ref: list = []
        commands = Commands(outputs)
        locks = Locks(ref)
        q = JobQueue(tmp_path / ".mcp-jobs.json", run_command=commands, locks=locks, **kw)
        ref.append(q)
        return q, commands, locks
    return make


def run(q: JobQueue, **kw) -> dict:
    defaults = dict(tool="embed_covers", args={}, mode="dry_run", caller="sub-1", tier="write",
                    commands=[["music-embed-covers"]])
    job = q.submit(**{**defaults, **kw})
    return q.wait(job["job_id"], 5)


def test_dry_run_records_its_output_and_hash(make, tmp_path):
    q, commands, _ = make({"music-embed-covers": (["[2026-10-27T10:00:00+0000] 3 item(s) would get a cover"], 0)})
    job = run(q)
    assert job["status"] == "done" and job["exit_code"] == 0
    assert job["dry_run_hash"] == output_hash(["3 item(s) would get a cover"])
    assert q.output(job["job_id"]) == ["[2026-10-27T10:00:00+0000] 3 item(s) would get a cover"]
    saved = json.loads((tmp_path / ".mcp-jobs.json").read_text())["jobs"][job["job_id"]]
    assert saved["status"] == "done" and saved["caller"] == "sub-1" and saved["tier"] == "write"
    assert (tmp_path / ".mcp-jobs" / f"{job['job_id']}.log").exists()


def test_failed_command_fails_the_job_and_sets_no_hash(make):
    q, _, _ = make({"music-embed-covers": (["boom"], 1)})
    job = run(q)
    assert job["status"] == "failed" and job["exit_code"] == 1 and job["dry_run_hash"] is None


def test_apply_reruns_the_dry_run_under_the_locks_and_applies_on_a_match(make):
    q, commands, locks = make({"music-album-reset K": (["[2026-10-27T10:00:00+0000] K: status wanted"], 0),
                               "music-album-reset K --apply": (["Saved"], 0)})
    dry = run(q, tool="reset_album", commands=[["music-album-reset", "K"]])
    job = run(q, tool="reset_album", mode="apply", commands=[["music-album-reset", "K"]],
              apply_commands=[["music-album-reset", "K", "--apply"]], expected_hash=dry["dry_run_hash"],
              locks=("albums", "pipeline"))
    assert job["status"] == "done"
    assert commands.ran[-2:] == [["music-album-reset", "K"], ["music-album-reset", "K", "--apply"]]
    assert locks.held[-1] == ("albums", "pipeline")
    assert locks.status_on_enter == ["waiting_for_lock"]
    assert commands.envs[-1]["MUSIC_LOCKS_HELD"] == "albums,pipeline"
    assert q.output(job["job_id"])[-1] == "Saved"
    assert job["argv"] == [["music-album-reset", "K", "--apply"]]


def test_apply_refuses_when_the_fresh_dry_run_differs(make):
    q, commands, _ = make({"music-album-reset K": (["K: status wanted"], 0)})
    job = run(q, tool="reset_album", mode="apply", commands=[["music-album-reset", "K"]],
              apply_commands=[["music-album-reset", "K", "--apply"]], expected_hash="sha256:old",
              locks=("albums", "pipeline"))
    assert job["status"] == "refused"
    assert ["music-album-reset", "K", "--apply"] not in commands.ran
    assert job["dry_run_hash"] == output_hash(["K: status wanted"])
    assert q.output(job["job_id"])[0] == "K: status wanted"
    # The refused run is now the dry run to apply.
    assert q.find_dry_run("reset_album", {})["job_id"] == job["job_id"]


def test_a_lockless_job_never_passes_music_locks_held(make, monkeypatch):
    monkeypatch.setenv("MUSIC_LOCKS_HELD", "pipeline")
    q, commands, _ = make()
    run(q)
    assert "MUSIC_LOCKS_HELD" not in commands.envs[-1]


def test_find_dry_run_matches_tool_and_args_within_the_window(make):
    q, _, _ = make()
    job = run(q, tool="canon_albums", args={"refresh": False, "mb_budget": None})
    assert q.find_dry_run("canon_albums", {"mb_budget": None, "refresh": False})["job_id"] == job["job_id"]
    assert q.find_dry_run("canon_albums", {"mb_budget": 5, "refresh": False}) is None
    assert q.find_dry_run("embed_covers", {}) is None
    q.jobs[job["job_id"]]["finished_at"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    assert q.find_dry_run("canon_albums", {"mb_budget": None, "refresh": False}) is None


def test_a_full_queue_is_busy(make):
    release = threading.Event()

    def blocking(argv, env, on_line):
        release.wait(5)
        return 0

    q, _, _ = make(depth=2)
    q._run_command = blocking
    first = q.submit("embed_covers", {}, "dry_run", "a", "write", [["x"]])
    q.submit("embed_covers", {}, "dry_run", "a", "write", [["x"]])
    with pytest.raises(mcp_jobs.Busy) as exc:
        q.submit("embed_covers", {}, "dry_run", "a", "write", [["x"]])
    assert [j["job_id"] for j in exc.value.queue][0] == first["job_id"]
    release.set()
    assert q.wait(first["job_id"], 5)["status"] == "done"


def test_restart_marks_queued_and_running_jobs_interrupted(tmp_path):
    path = tmp_path / ".mcp-jobs.json"
    stamp = mcp_jobs.now()
    jobs = {i: {"job_id": i, "tool": "embed_covers", "status": s, "created_at": stamp, "caller": "c"}
            for i, s in [("a", "running"), ("b", "queued"), ("c", "waiting_for_lock"), ("d", "done")]}
    path.write_text(json.dumps({"jobs": jobs}))
    q = JobQueue(path)
    assert {i: j["status"] for i, j in q.jobs.items()} == {
        "a": "interrupted", "b": "interrupted", "c": "interrupted", "d": "done"}
    assert json.loads(path.read_text())["jobs"]["a"]["status"] == "interrupted"


def test_prune_keeps_the_last_50_or_the_last_7_days(tmp_path):
    path = tmp_path / ".mcp-jobs.json"
    old = datetime.now(timezone.utc) - timedelta(days=30)
    jobs = {f"old{n:02}": {"job_id": f"old{n:02}", "status": "done",
                           "created_at": (old + timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            for n in range(60)}
    path.write_text(json.dumps({"jobs": jobs}))
    q = JobQueue(path, run_command=Commands(), locks=lambda names: contextlib.nullcontext())
    run(q)
    assert len(q.jobs) == 50 and "old00" not in q.jobs and "old59" in q.jobs
    # 60 recent ones: every one is kept.
    recent = {f"new{n:02}": {"job_id": f"new{n:02}", "status": "done", "created_at": mcp_jobs.now()}
              for n in range(60)}
    path.write_text(json.dumps({"jobs": recent}))
    q = JobQueue(path, run_command=Commands(), locks=lambda names: contextlib.nullcontext())
    run(q)
    assert len(q.jobs) == 61


def test_output_hash_ignores_log_timestamps_only():
    a = ["[2026-10-27T10:00:00+0000] K: status failed → wanted", "2026-10-27T10:00:01Z"]
    b = ["[2026-10-28T11:30:00+0000] K: status failed → wanted"]
    assert output_hash(a) == output_hash(b)
    assert output_hash(a) != output_hash(["[2026-10-27T10:00:00+0000] K: status failed → imported"])


def test_page_caps_lines_and_bytes():
    lines = [f"line {n}" for n in range(450)]
    text, nxt = page(lines, 0, finished=True)
    assert len(text.splitlines()) == 200 and nxt == 200
    text, nxt = page(lines, 400, finished=True)
    assert len(text.splitlines()) == 50 and nxt is None
    assert page(lines, 450, finished=False) == ("", 450)  # still running: keep polling
    big = ["x" * 20000, "y" * 20000]
    text, nxt = page(big, 0, finished=True)
    assert text == "x" * 20000 and nxt == 1
    text, _ = page(["z" * 40000], 0, finished=True)
    assert len(text.encode()) <= 32 * 1024 and text.endswith("…")


# ---------------------------------------------------------------------------
# MUSIC_LOCKS_HELD: only ever in a job subprocess's environment
# ---------------------------------------------------------------------------


def test_cli_locks_skip_only_what_music_locks_held_names(monkeypatch):
    import prefect.concurrency.sync as sync

    from music_fetch import albums
    from music_scan import audit

    taken = []

    @contextlib.contextmanager
    def concurrency(name, **kw):
        taken.append(name)
        yield

    monkeypatch.setattr(sync, "concurrency", concurrency)
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect")
    monkeypatch.setenv("MUSIC_LOCKS_HELD", "albums,pipeline")
    with audit.pipeline_lock(), albums._state_lock(True):
        pass
    assert taken == []
    monkeypatch.delenv("MUSIC_LOCKS_HELD")
    with audit.pipeline_lock(), albums._state_lock(True):
        pass
    assert taken == ["pipeline", "albums"]


def test_the_service_refuses_to_start_with_music_locks_held(monkeypatch):
    from music_service import cli

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "API_BEARER_TOKEN"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("MUSIC_LOCKS_HELD", "pipeline")
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


def test_the_image_does_not_set_music_locks_held():
    # The process's environment as it started (an image ENV included), before
    # any test's monkeypatch.
    assert "MUSIC_LOCKS_HELD" not in _START_ENV

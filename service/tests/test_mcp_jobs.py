"""Tests for music_service.mcp_jobs — the MCP job queue (homelab#2412)."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

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
        # A job resumed by the constructor can get here before the queue is in queue_ref.
        jobs = self.queue_ref[0].jobs if self.queue_ref else {}
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


@pytest.mark.parametrize("job, expected", [
    ({"mode": "dry_run", "status": "running"}, None),
    ({"mode": "apply", "status": "queued"}, None),
    ({"mode": "apply", "status": "waiting_for_lock"}, None),
    ({"mode": "apply", "status": "running"}, mcp_jobs.CHECKING_DRY_RUN),
    ({"mode": "apply", "status": "running", "applying": True}, mcp_jobs.APPLYING),
    ({"mode": "apply", "status": "done", "applying": True}, None),
    ({"mode": "apply", "status": "refused"}, None),
])
def test_phase_names_the_step_of_a_running_apply(job, expected):
    assert mcp_jobs.phase(job) == expected


def test_dry_run_records_its_output_and_hash(make, tmp_path):
    q, commands, _ = make({"music-embed-covers": (["[2026-10-27T10:00:00+0000] 3 item(s) would get a cover"], 0)})
    job = run(q)
    assert job["status"] == "done" and job["exit_code"] == 0
    assert job["dry_run_hash"] == output_hash(["3 item(s) would get a cover"])
    assert q.output(job["job_id"]) == ["[2026-10-27T10:00:00+0000] 3 item(s) would get a cover"]
    saved = json.loads((tmp_path / ".mcp-jobs.json").read_text())["jobs"][job["job_id"]]
    assert saved["status"] == "done" and saved["caller"] == "sub-1" and saved["tier"] == "write"
    assert (tmp_path / ".mcp-jobs" / f"{job['job_id']}.log").exists()


def test_a_musicbrainz_retry_is_not_part_of_the_dry_run_hash():
    """backfill_album_ids logged 9 retries on one dry run and 10 on the next, with the same
    links, and the apply was refused (homelab#2664)."""
    plain = ["[LINK] 1748 Razzamatazz", "Linked 1"]
    retried = ["[2026-11-05T19:50:00+0000]   [MB-503] /recording/x: MusicBrainz is busy; retrying in 1s", *plain]
    assert output_hash(retried) == output_hash(plain)
    assert output_hash(["[LINK] 1748 Velas", "Linked 1"]) != output_hash(plain)


@pytest.mark.parametrize("failed", [
    "MusicBrainz lookup of recording 9b73f625 failed: HTTP Error 503: Service Temporarily Unavailable",
    "MusicBrainz lookup of ISRC GBCVT9900017 failed: HTTP Error 503: Service Temporarily Unavailable",
    "MusicBrainz lookup of recording 55f5e765 failed: The read operation timed out",
])
def test_a_failed_musicbrainz_lookup_is_not_part_of_the_dry_run_hash(failed):
    """Retries ran out on another recording each run, with the same links, and the apply was
    refused (homelab#2700)."""
    plain = ["[LINK] 1748 fancy: Razzamatazz — Pulp → item 7 at 1-1", "Linked 1"]
    assert output_hash([f"[2026-11-07T12:12:32+0000] {failed}", *plain]) == output_hash(plain)
    assert output_hash(["[LINK] 1748 fancy: Velas — Pulp → item 7 at 1-1", "Linked 1"]) != output_hash(plain)


@pytest.mark.parametrize("line", [
    "[NOREFRESH] 4230: the MusicBrainz lookup of abc failed (HTTP Error 503); title and trackdisambig left",
    "  [MB-WAIT] 1748: MusicBrainz lookup failed, retried next run: HTTP Error 503",
    "--set-recording: the MusicBrainz lookup of abc failed (HTTP Error 503); nothing changed",
])
def test_a_failed_lookup_that_changes_what_a_tool_does_stays_in_the_dry_run_hash(line):
    """apply_recheck skips an item on [NOREFRESH]: only backfill's warning changes nothing by itself."""
    assert output_hash([line, "Linked 1"]) != output_hash(["Linked 1"])


SUMMARY = "Album IDs: {} imported album(s) read, {} empty entr(ies) to link, {} left empty ({})"


def test_the_albums_read_count_is_not_part_of_the_dry_run_hash():
    """It rises with every import, and a run outlasts the gap between imports (homelab#2700)."""
    read = output_hash([SUMMARY.format(1349, 2, 419, "419 no item")])
    assert output_hash([f"[2026-11-07T12:12:32+0000] {SUMMARY.format(1350, 2, 419, '419 no item')}"]) == read
    assert output_hash([SUMMARY.format(1349, 3, 419, "419 no item")]) != read, "the links are compared"
    assert output_hash([SUMMARY.format(1349, 2, 420, "420 no item")]) != read, "the left-empty count is compared"
    assert output_hash([SUMMARY.format(1349, 2, 419, "418 no item, 1 number, other isrc")]) != read


def _album_item(id_, key, track, **data):
    data |= {"via": "usenet", "album_record": key}
    item = MagicMock(id=id_, title=f"Song {track}", artist="Muse", albumartist="Muse", disc=1, track=track)
    item.get = lambda k, d=None: data.get(k, d)
    return item


def _backfill_output(caplog, recording, items, records) -> list[str]:
    """What ``music-backfill-ids --albums`` logs, a MusicBrainz *recording* read (a value, or an
    exception to raise) under the real client, so the hash is tested on its real lines."""
    from music_scan.backfill import mb_recordings, plan_album_ids, report_albums

    caplog.clear()
    read = {"side_effect": recording} if isinstance(recording, Exception) else {"return_value": recording}
    with patch("music_scan.mb_release.MusicBrainz.get", **read), caplog.at_level("INFO", logger="music_scan.backfill"):
        report_albums(plan_album_ids(items, records, mb_recordings()))
    return list(caplog.messages)


def _says(lines: list[str], text: str) -> bool:
    return any(text in line for line in lines)


BUSY = HTTPError("https://musicbrainz.org", 503, "Service Temporarily Unavailable", Message(), None)
ENTRY = {"status": "imported", "playlists": {"fancy": [["Song 4", "Muse", "S1", "ENTRY", 1, 4]]}}


def test_backfills_real_output_hashes_alike_when_only_the_lookup_and_the_count_differ(caplog):
    items, records = [_album_item(1, "10", 4, isrc="ITEM", mb_trackid="r1")], {"10": ENTRY}
    told = _backfill_output(caplog, {"isrcs": ["ITEM"]}, items, records)
    busy = _backfill_output(caplog, BUSY, items, records)
    assert _says(told, "[NOID] 10 fancy") and _says(busy, "lookup of recording r1 failed")
    assert output_hash(busy) == output_hash(told)

    records = {**records, "11": {"status": "imported", "playlists": {}}}
    more = _backfill_output(caplog, {"isrcs": ["ITEM"]}, [*items, _album_item(2, "11", 1)], records)
    assert _says(more, "Album IDs: 2 imported") and output_hash(more) == output_hash(told)


def test_a_failed_lookup_that_loses_a_link_still_changes_the_hash(caplog):
    """A failed lookup refutes nothing, so the entry a good lookup would link shows as [NOID]."""
    items, records = [_album_item(1, "10", 4, isrc="ITEM", mb_trackid="r1")], {"10": ENTRY}
    linked = _backfill_output(caplog, {"isrcs": ["ITEM", "ENTRY"]}, items, records)
    lost = _backfill_output(caplog, BUSY, items, records)
    assert _says(linked, "[LINK] 10 fancy") and _says(lost, "[NOID] 10 fancy")
    assert output_hash(lost) != output_hash(linked)


def test_a_finished_job_records_an_mcp_job_event(make, monkeypatch):
    from music_service import mcp_jobs

    recorded = []
    monkeypatch.setattr(mcp_jobs.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    q, _, _ = make({"music-embed-covers": (["boom"], 1)})
    job = run(q)
    (kind, message, kw), = recorded
    assert kind == "mcp_job" and message == f"[MCP] job={job['job_id']} end status=failed exit=1 " \
                                            "tool=embed_covers caller=sub-1"
    assert kw["caller"] == "sub-1" and kw["job_id"] == job["job_id"] and kw["source"] == "mcp"
    assert kw["data"]["tool"] == "embed_covers" and kw["data"]["status"] == "failed"


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
    assert commands.envs[-1]["MUSIC_EVENT_JOB"] == job["job_id"]
    assert commands.envs[-1]["MUSIC_EVENT_CALLER"] == job["caller"]
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


# --- the long lane and resumes (homelab#2553) ---


def test_a_long_job_runs_beside_the_short_lane(make):
    """An hour-long recheck doesn't hold up the short tools, and a second long job is busy."""
    release = threading.Event()
    started = threading.Event()

    def commands(argv, env, on_line):
        if argv[0] == "music-mb-fingerprint":
            started.set()
            release.wait(5)
        on_line(f"ran {' '.join(argv)}")
        return 0

    q, _, _ = make()
    q._run_command = commands
    long_job = q.submit("recheck_recordings", {}, "dry_run", "pilot", "read",
                        [["music-mb-fingerprint", "--recheck", f"--run-id={mcp_jobs.JOB_ID}"]],
                        lane=mcp_jobs.LONG, resumable=True)
    assert long_job["argv"] == [["music-mb-fingerprint", "--recheck", f"--run-id={long_job['job_id']}"]]
    assert started.wait(5)
    short = run(q)  # finishes while the long job runs
    assert short["status"] == "done" and short["lane"] == mcp_jobs.SHORT
    with pytest.raises(mcp_jobs.Busy) as exc:
        q.submit("recheck_recordings", {}, "dry_run", "pilot", "read", [["music-mb-fingerprint"]],
                 lane=mcp_jobs.LONG)
    assert [j["job_id"] for j in exc.value.queue] == [long_job["job_id"]]
    assert [j["job_id"] for j in q.active(mcp_jobs.LONG)] == [long_job["job_id"]]
    assert q.active(mcp_jobs.SHORT) == []
    release.set()
    assert q.wait(long_job["job_id"], 5)["status"] == "done"


def _left_running(path, **job):
    """A records file as a restart leaves it: *job* still running, with a log."""
    stamp = mcp_jobs.now()
    record = {"job_id": "j1", "tool": "recheck_recordings", "args": {}, "mode": "dry_run", "status": "running",
              "caller": "sub-1", "tier": "read", "argv": [["music-mb-fingerprint", "--recheck", "--run-id=j1"]],
              "locks": [], "lane": mcp_jobs.LONG, "resumable": True, "resumes": 0, "created_at": stamp,
              "started_at": stamp, "finished_at": None, "exit_code": None, "dry_run_hash": None, "error": None,
              **job}
    path.write_text(json.dumps({"jobs": {record["job_id"]: record}}))
    (path.parent / ".mcp-jobs").mkdir(exist_ok=True)
    (path.parent / ".mcp-jobs" / f"{record['job_id']}.log").write_text(
        "[2026-11-01T10:00:00+0000] Rechecking 9210 item(s)\n"
        "[2026-11-01T10:03:00+0000] [PROGRESS] 500/9210, 9 flagged so far\n")
    return record


def _restart(path, commands=None):
    ref: list = []
    commands = commands or Commands()
    locks = Locks(ref)
    q = JobQueue(path, run_command=commands, locks=locks)
    ref.append(q)
    return q, commands, locks


def test_a_restart_resumes_a_long_job_with_its_log(tmp_path):
    path = tmp_path / ".mcp-jobs.json"
    _left_running(path)
    q, commands, _ = _restart(path)
    job = q.wait("j1", 5)
    assert job["status"] == "done" and job["resumes"] == 1 and job["resumed_at"]
    assert commands.ran == [["music-mb-fingerprint", "--recheck", "--run-id=j1"]]  # its CLI resumes the run
    out = q.output("j1")
    assert out[:2] == ["[2026-11-01T10:00:00+0000] Rechecking 9210 item(s)",
                       "[2026-11-01T10:03:00+0000] [PROGRESS] 500/9210, 9 flagged so far"]
    assert out[2] == "[MCP] resumed after a restart (1 of 5)"
    assert out[3] == "ran music-mb-fingerprint --recheck --run-id=j1"


def test_a_restart_resumes_an_apply_under_its_locks_without_the_comparison(tmp_path):
    """A resumed apply takes its locks again and runs only its --apply commands: the CLI
    re-checks each item (music-mb-fingerprint --replace-run), and the dry run it matched
    before the restart is not re-run, so a partly applied run is never refused."""
    path = tmp_path / ".mcp-jobs.json"
    _left_running(path, tool="apply_recheck", mode="apply", tier="write", lane=mcp_jobs.SHORT, applying=True,
                  argv=[["music-mb-fingerprint", "--replace-run=r1", "--apply"]],
                  dry_run_argv=[["music-mb-fingerprint", "--replace-run=r1"]], locks=["pipeline"])
    q, commands, locks = _restart(path)
    job = q.wait("j1", 5)
    assert job["status"] == "done" and job["resumes"] == 1
    assert locks.held == [("pipeline",)]
    assert commands.ran == [["music-mb-fingerprint", "--replace-run=r1", "--apply"]]
    assert commands.envs[0]["MUSIC_LOCKS_HELD"] == "pipeline"
    assert job["dry_run_hash"] is None  # not a dry run anyone may apply
    assert q.output("j1")[2].startswith("[MCP] resumed after a restart (1 of 5); the dry run matched before it")


@pytest.mark.parametrize("job", [
    {},
    {"tool": "apply_recheck", "mode": "apply", "tier": "write", "lane": mcp_jobs.SHORT, "locks": ["pipeline"],
     "applying": True, "argv": [["music-mb-fingerprint", "--replace-run=r1", "--apply"]]},
], ids=["long job", "apply"])
def test_the_resume_cap_interrupts_a_job_restarted_too_often(tmp_path, job):
    path = tmp_path / ".mcp-jobs.json"
    _left_running(path, resumes=mcp_jobs.MAX_RESUMES, **job)
    q, commands, locks = _restart(path)
    record = q.get("j1")
    assert record["status"] == "interrupted" and record["resumes"] == mcp_jobs.MAX_RESUMES
    assert record["error"] == "the service restarted; resumed 5 times already, not again"
    assert commands.ran == [] and locks.held == []


def test_a_job_that_is_not_resumable_is_still_interrupted(tmp_path):
    path = tmp_path / ".mcp-jobs.json"
    _left_running(path, tool="embed_covers", resumable=False, lane=mcp_jobs.SHORT)
    q, commands, _ = _restart(path)
    assert q.get("j1")["status"] == "interrupted" and commands.ran == []


def test_progress_is_the_latest_progress_line():
    lines = ["[2026-11-01T10:03:00+0000] [PROGRESS] 500/9210, 9 flagged so far",
             "[2026-11-01T10:03:01+0000] [RECHECK] 812 — …",
             "[2026-11-01T10:06:00+0000] [PROGRESS] 1000/9210, 20 flagged so far",
             "[2026-11-01T10:06:01+0000] [NOMATCH] 990 — …"]
    assert mcp_jobs.progress(lines) == {"done": 1000, "total": 9210, "at": "2026-11-01T10:06:00+0000",
                                        "line": "[PROGRESS] 1000/9210, 20 flagged so far"}
    assert mcp_jobs.progress(["[PROGRESS] 3/4, 1 flagged so far"])["at"] is None
    assert mcp_jobs.progress(["ran x"]) is None


@pytest.mark.parametrize("status", ["queued", "waiting_for_lock", "running"])
def test_an_apply_cut_short_before_its_comparison_is_not_resumed(tmp_path, status):
    """Its dry run was never compared, so resuming it would apply unguarded: interrupted, nothing applied."""
    path = tmp_path / ".mcp-jobs.json"
    _left_running(path, tool="apply_recheck", mode="apply", tier="write", lane=mcp_jobs.SHORT, status=status,
                  argv=[["music-mb-fingerprint", "--replace-run=r1", "--apply"]], locks=["pipeline"])
    q, commands, locks = _restart(path)
    record = q.get("j1")
    assert record["status"] == "interrupted" and commands.ran == [] and locks.held == []
    assert record["error"] == "the service restarted before the apply began; nothing was applied"


def test_an_apply_marks_itself_applying_once_its_comparison_passes(make):
    q, _, _ = make({"music-mb-fingerprint --replace-run=r1": (["[WOULD-REPLACE] 1"], 0)})
    dry = run(q, tool="apply_recheck", commands=[["music-mb-fingerprint", "--replace-run=r1"]])
    assert not dry.get("applying")
    job = run(q, tool="apply_recheck", mode="apply", commands=[["music-mb-fingerprint", "--replace-run=r1"]],
              apply_commands=[["music-mb-fingerprint", "--replace-run=r1", "--apply"]],
              expected_hash=dry["dry_run_hash"], locks=("pipeline",), resumable=True)
    assert job["status"] == "done" and job["applying"] is True and job["resumable"] is True

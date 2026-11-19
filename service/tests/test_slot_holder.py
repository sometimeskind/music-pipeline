"""A run that finds albums or pipeline held names the holder, and the service
exports long holds for MusicSlotHeldLong (homelab#2841)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from unittest.mock import MagicMock, patch

import pytest

from music_service import flows, slots


@pytest.fixture(autouse=True)
def _slots_dir(tmp_path_factory, monkeypatch):
    """These tests write other runs' records: keep them out of ``/tmp/music-slots``."""
    monkeypatch.setattr(slots, "SLOTS_DIR", tmp_path_factory.mktemp("music-slots"))


# 2026-11-12 13:14:20Z, the day tomato-gaur held albums for nearly five hours.
SINCE = 1794489260.0


def _record(slot: str = "albums", flow: str = "music-album-import", run: str = "tomato-gaur",
            since: float = SINCE, pid: int | None = None) -> slots.Holder:
    """Write another run's record of *slot*, as its :func:`slots.held` would."""
    holder = slots.Holder(slot=slot, flow=flow, run=run, since=since, pid=os.getpid() if pid is None else pid,
                          token="other")
    slots.SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    (slots.SLOTS_DIR / f"{slot}.json").write_text(json.dumps(asdict(holder)))
    return holder


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _info_lines(logger) -> list[str]:
    return [c.args[0] % c.args[1:] for c in logger.return_value.info.call_args_list]


def test_a_held_slot_records_its_holder_until_it_is_let_go():
    with patch.object(slots, "_run_names", return_value=("music-album-import", "tomato-gaur")):
        with slots.held("albums"):
            holder = slots.holder_of("albums")
            assert holder is not None
            assert (holder.slot, holder.flow, holder.run) == ("albums", "music-album-import", "tomato-gaur")
            assert holder.alive()
            assert time.time() - holder.since < 5
    assert slots.holder_of("albums") is None


def test_letting_go_leaves_another_run_s_record():
    """A run that outlived its lease finds the next holder's record: it keeps it."""
    with slots.held("pipeline", "music-scan", "large-owl"):
        other = _record("pipeline", "music-fetch", "shrewd-polecat")
    assert slots.holder_of("pipeline") == other


def test_the_holder_is_named_with_its_flow_run_and_age():
    holder = _record()
    assert holder.describe(now=SINCE + 4 * 3600 + 52 * 60 + 7) == (
        "music-album-import run 'tomato-gaur', held since 2026-11-12 13:14:20Z (4h 52m)")
    assert holder.describe(now=SINCE + 125) == "music-album-import run 'tomato-gaur', held since 2026-11-12 13:14:20Z (2 min)"


def test_a_holder_whose_process_exited_says_the_lease_frees_the_slot():
    holder = _record(pid=_dead_pid())
    assert not holder.alive()
    assert holder.describe(now=SINCE + 60).endswith(
        "; its process has exited, so Prefect frees the slot when the lease runs out (5 min)")


def test_no_record_says_so():
    assert slots.describe("albums") == "no holder recorded: a CLI run in the pod?"


def test_hold_records_the_run_as_the_holder_while_it_holds_the_slot():
    with patch.object(flows, "concurrency"), patch.object(flows, "get_run_logger"), \
         patch.object(slots, "_run_names", return_value=("music-albums", "abiding-elephant")):
        with flows.hold("albums"):
            assert slots.holder_of("albums").run == "abiding-elephant"
        assert slots.holder_of("albums") is None


def test_a_wait_for_a_held_slot_names_the_holder():
    """Before, the line said only "held by another run" (homelab#2841)."""
    _record(since=time.time() - 25 * 60)
    busy, free = MagicMock(), MagicMock()
    busy.__enter__.side_effect = TimeoutError
    with patch.object(flows, "concurrency", side_effect=[busy, free]), \
         patch.object(flows, "get_run_logger") as logger:
        with flows.hold("albums"):
            pass
    first = _info_lines(logger)[0]
    assert first.startswith("[WAIT] The albums slot is held by another run (music-album-import run 'tomato-gaur', "
                            "held since ")
    assert first.endswith(" (25 min)); waiting for it")


def test_a_skipped_album_tick_names_the_holder():
    import music_fetch.albums as albums

    _record(since=time.time() - (2 * 3600 + 5 * 60))
    with patch.object(albums.Settings, "from_env", return_value=albums.Settings(mode="on")), \
         patch.object(flows, "hold", side_effect=flows.Busy("albums")), \
         patch.object(flows, "album_tick_task") as task, \
         patch.object(flows, "get_run_logger") as logger:
        flows.albums_flow.fn()
    task.assert_not_called()
    (line,) = _info_lines(logger)
    assert line.startswith("Album tick skipped — another album run held the lock for 20 min "
                           "(music-album-import run 'tomato-gaur', held since ")
    assert line.endswith(" (2h 05m))")


def test_a_busy_sync_probe_names_the_pipeline_holder():
    _record("pipeline", "music-fetch", "shrewd-polecat", since=time.time() - 3 * 60)
    with patch.object(flows, "concurrency") as slot, patch.object(flows, "get_run_logger") as logger:
        slot.return_value.__enter__.side_effect = TimeoutError
        assert flows._pipeline_free() is False
    (line,) = _info_lines(logger)
    assert line.startswith("[SYNC] The pipeline slot is held (music-fetch run 'shrewd-polecat', held since ")


def test_a_scan_that_takes_the_free_pipeline_slot_records_itself():
    """music-scan takes pipeline with its own probe, not through hold()."""
    seen = []
    with patch("music_fetch.state.check"), patch.object(flows, "concurrency"), \
         patch.object(flows, "get_run_logger"), \
         patch.object(slots, "_run_names", return_value=("music-scan", "large-owl")), \
         patch.object(flows, "_run_scan_tasks", side_effect=lambda: seen.append(slots.holder_of("pipeline").run)):
        flows.scan_flow.fn()
    assert seen == ["large-owl"]
    assert slots.holder_of("pipeline") is None


def test_an_mcp_job_records_itself_as_the_holder_of_its_locks(tmp_path):
    from music_service.mcp_jobs import JobQueue

    seen = []

    def command(argv, env, on_line):
        seen.append({s: (h.flow, h.run) for s in slots.SLOTS if (h := slots.holder_of(s))})
        return 0

    q = JobQueue(tmp_path / ".mcp-jobs.json", run_command=command, locks=lambda names: contextlib.nullcontext())
    job = q.submit(tool="requeue_albums", args={}, mode="dry_run", caller="sub-1", tier="write",
                   commands=[["music-album-requeue"]], locks=("albums", "pipeline"))
    assert q.wait(job["job_id"], 5)["status"] == "done"
    assert seen == [{"albums": ("mcp-job requeue_albums", job["job_id"]),
                     "pipeline": ("mcp-job requeue_albums", job["job_id"])}]
    assert slots.holder_of("albums") is None and slots.holder_of("pipeline") is None


def test_the_metrics_name_a_long_hold_and_not_a_short_one():
    now = SINCE + 3 * 3600
    _record("albums", since=SINCE)
    _record("pipeline", "music-scan", "large-owl", since=now - 60)
    assert slots.metrics_body(now=now) == "\n".join([
        "# TYPE music_slot_held gauge",
        'music_slot_held{slot="albums"} 1',
        'music_slot_held{slot="pipeline"} 1',
        "# TYPE music_slot_held_since_timestamp_seconds gauge",
        'music_slot_held_since_timestamp_seconds{slot="albums",flow="music-album-import",run="tomato-gaur"} '
        f"{int(SINCE)}",
    ])


def test_the_metrics_leave_out_free_slots_and_dead_holders():
    _record("albums", pid=_dead_pid())
    assert slots.metrics_body(now=SINCE + 5 * 3600) == "\n".join([
        "# TYPE music_slot_held gauge",
        'music_slot_held{slot="albums"} 0',
        'music_slot_held{slot="pipeline"} 0',
    ])


def test_a_label_value_is_escaped():
    holder = replace(_record(), flow='mcp-job "x"')
    assert slots._label(holder.flow) == 'mcp-job \\"x\\"'


def test_the_reporter_pushes_to_its_own_job():
    pushed = []
    with patch("music_fetch.metrics._push", side_effect=lambda body, job: pushed.append(job)):
        slots.push()
    assert pushed == [slots.METRICS_JOB] == ["music_slots"]


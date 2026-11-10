"""The service entry point's deployment schedules (homelab#2596)."""

import asyncio
from datetime import datetime, timedelta, timezone

from prefect.client.schemas.actions import DeploymentScheduleCreate
from prefect.server.schemas.schedules import IntervalSchedule

from music_service.cli import FIRST_ALBUM_TICK_SECONDS, interval_schedule

START = datetime(2026, 11, 2, 11, 50, 35, tzinfo=timezone.utc)


def _server_dates(schedule, start, n=3):
    """The dates Prefect's server schedules from what ``serve()`` sends it."""
    sent = DeploymentScheduleCreate.from_schedule(schedule).schedule
    server = IntervalSchedule(interval=sent.interval, anchor_date=sent.anchor_date)
    return asyncio.run(server.get_dates(n=n, start=start))


def test_first_album_tick_runs_a_minute_after_start_not_an_interval():
    # The server schedules a deployment a few seconds after serve() upserts it.
    dates = _server_dates(interval_schedule(1800, now=START), START + timedelta(seconds=5))

    assert dates[0] == START + timedelta(seconds=FIRST_ALBUM_TICK_SECONDS)
    assert dates[1] - dates[0] == timedelta(seconds=1800)


def test_without_an_anchor_the_first_tick_is_a_whole_interval_away():
    # The bug: Prefect anchors an interval= schedule at upsert time.
    from prefect.schedules import Interval

    dates = _server_dates(Interval(1800, anchor_date=START), START + timedelta(seconds=5), n=1)

    assert dates[0] == START + timedelta(seconds=1800)


def test_startup_scan_runs_after_resume_and_the_watcher_before_serve(monkeypatch):
    """homelab#2751: the startup scan counts the inbox only once an interrupted
    album import is parked back (#344) and the watcher runs, and before serve() blocks.
    homelab#2763: an interrupted as-is pass's staged files go back to quarantine
    first, before the API or the watcher can start a scan."""
    from unittest.mock import MagicMock, patch

    from music_service import cli

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "API_BEARER_TOKEN"):
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("MUSIC_LOCKS_HELD", raising=False)
    order: list[str] = []

    def step(name):
        return lambda *a, **k: order.append(name) or MagicMock()

    with patch("music_fetch.albums.park_interrupted_imports", side_effect=step("resume")), \
         patch.object(cli, "resume_asis_staging", side_effect=step("asis")), \
         patch("music_service.api.create_app", side_effect=step("api")), \
         patch("music_service.mcp_jobs.JobQueue"), \
         patch.object(cli, "make_api_server"), \
         patch.object(cli, "_start_file_watcher", side_effect=step("watcher")), \
         patch("music_service.prefect_client.ensure_concurrency_limits"), \
         patch.object(cli, "schedule_startup_scan", side_effect=step("startup-scan")), \
         patch("prefect.serve", side_effect=step("serve")):
        cli.main()

    assert order == ["resume", "asis", "api", "watcher", "startup-scan", "serve"]


def test_resume_asis_staging_moves_the_staged_files_back_and_logs_counts(tmp_path, monkeypatch, caplog):
    """homelab#2763: counts only, never a path or a title."""
    from music_scan import scan
    from music_service import cli

    quarantine = tmp_path / "quarantine"
    staging, apart = tmp_path / "asis-staging", tmp_path / "asis-apart"
    for f in (staging / "spotdl/p/secret-one.m4a", staging / "spotdl/p/secret-two.m4a", apart / "spotdl/p/secret-three.m4a",
              quarantine / "spotdl/p/secret-two.m4a"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(f.name.encode())
    monkeypatch.setattr(scan, "QUARANTINE", quarantine)

    with caplog.at_level("INFO"):
        assert cli.resume_asis_staging() == 3

    assert sorted(p.name for p in quarantine.rglob("*.m4a")) == [
        "secret-one.m4a", "secret-three.m4a", "secret-two (restored).m4a", "secret-two.m4a"]
    assert "[RESUME] 3 as-is file(s) back to quarantine" in caplog.text
    assert "1 kept beside a file of the same name" in caplog.text
    assert "secret" not in caplog.text and "spotdl" not in caplog.text
    caplog.clear()
    assert cli.resume_asis_staging() == 0
    assert "[RESUME]" not in caplog.text


def test_resume_asis_staging_never_raises(monkeypatch, caplog):
    from music_scan import scan
    from music_service import cli

    monkeypatch.setattr(scan, "restore_asis_staging", lambda: (_ for _ in ()).throw(PermissionError("denied")))

    assert cli.resume_asis_staging() == 0
    assert "they stay staged" in caplog.text

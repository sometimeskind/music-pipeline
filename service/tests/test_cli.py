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
    album import is parked back (#344) and the watcher runs, and before serve() blocks."""
    from unittest.mock import MagicMock, patch

    from music_service import cli

    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "API_BEARER_TOKEN"):
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv("MUSIC_LOCKS_HELD", raising=False)
    order: list[str] = []

    def step(name):
        return lambda *a, **k: order.append(name) or MagicMock()

    with patch("music_fetch.albums.park_interrupted_imports", side_effect=step("resume")), \
         patch("music_service.api.create_app"), \
         patch("music_service.mcp_jobs.JobQueue"), \
         patch.object(cli, "make_api_server"), \
         patch.object(cli, "_start_file_watcher", side_effect=step("watcher")), \
         patch("music_service.prefect_client.ensure_concurrency_limits"), \
         patch.object(cli, "schedule_startup_scan", side_effect=step("startup-scan")), \
         patch("prefect.serve", side_effect=step("serve")):
        cli.main()

    assert order == ["resume", "watcher", "startup-scan", "serve"]

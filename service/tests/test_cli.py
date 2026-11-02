"""The service entry point's deployment schedules (homelab#2596)."""

import asyncio
from datetime import datetime, timedelta, timezone

from prefect.client.schemas.actions import DeploymentScheduleCreate
from prefect.server.schemas.schedules import IntervalSchedule

from music_service.cli import FIRST_ALBUM_TICK_SECONDS, album_schedule

START = datetime(2026, 11, 2, 11, 50, 35, tzinfo=timezone.utc)


def _server_dates(schedule, start, n=3):
    """The dates Prefect's server schedules from what ``serve()`` sends it."""
    sent = DeploymentScheduleCreate.from_schedule(schedule).schedule
    server = IntervalSchedule(interval=sent.interval, anchor_date=sent.anchor_date)
    return asyncio.run(server.get_dates(n=n, start=start))


def test_first_album_tick_runs_a_minute_after_start_not_an_interval():
    # The server schedules a deployment a few seconds after serve() upserts it.
    dates = _server_dates(album_schedule(1800, now=START), START + timedelta(seconds=5))

    assert dates[0] == START + timedelta(seconds=FIRST_ALBUM_TICK_SECONDS)
    assert dates[1] - dates[0] == timedelta(seconds=1800)


def test_without_an_anchor_the_first_tick_is_a_whole_interval_away():
    # The bug: Prefect anchors an interval= schedule at upsert time.
    from prefect.schedules import Interval

    dates = _server_dates(Interval(1800, anchor_date=START), START + timedelta(seconds=5), n=1)

    assert dates[0] == START + timedelta(seconds=1800)

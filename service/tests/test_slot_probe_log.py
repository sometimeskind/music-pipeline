"""The skip-if-busy slot probe's timeout logs no Prefect ERROR (homelab#2596).

These run Prefect's real acquisition service against a fake API client, so the
record checked is the one Prefect writes, from its own loop thread."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from unittest.mock import patch

import httpx
import pytest
from prefect.concurrency.services import ConcurrencySlotAcquisitionWithLeaseService
from prefect.concurrency.sync import concurrency

from music_service import flows

PREFECT_LOGGER = "prefect._internal.concurrency"


class _Client:
    """Stands in for the PrefectClient: the acquire request hangs (a held slot,
    retried until the caller's timeout) or fails as an unreachable server."""

    def __init__(self, fail: Exception | None):
        self.fail = fail

    async def increment_concurrency_slots_with_lease(self, **_kwargs):
        if self.fail is not None:
            raise self.fail
        await asyncio.sleep(30)


def _probe(caplog, fail: Exception | None) -> tuple[BaseException, list[logging.LogRecord]]:
    """Probe a fresh limit as ``hold`` does; returns what it raised and the
    ERROR records Prefect's concurrency logger kept (caplog sees them by
    propagation; the unfiltered test shows that it does)."""
    name = f"probe-{uuid.uuid4().hex}"

    @contextlib.asynccontextmanager
    async def get_client():
        yield _Client(fail)

    with patch("prefect.concurrency.services.get_client", get_client), caplog.at_level(logging.ERROR):
        with pytest.raises(Exception) as raised:
            with concurrency(name, occupy=1, timeout_seconds=0.2):
                pass
        # The service logs after waking the caller; wait for it to finish the item.
        ConcurrencySlotAcquisitionWithLeaseService.instance(frozenset([name])).wait_until_empty()
    errors = [r for r in caplog.records if r.name == PREFECT_LOGGER and r.levelno >= logging.ERROR]
    return raised.value, errors


def test_a_timed_out_probe_logs_no_prefect_error(caplog):
    raised, errors = _probe(caplog, fail=None)
    assert isinstance(raised, TimeoutError)  # the caller still gets the timeout
    assert errors == []


def test_without_the_filter_prefect_logs_the_timeout(caplog):
    """The record the filter drops is real: guards against a vacuous pass if
    Prefect changes how it reports the failure."""
    logger = logging.getLogger(PREFECT_LOGGER)
    quiet = [f for f in logger.filters if isinstance(f, flows._QuietSlotTimeout)]
    assert quiet
    for f in quiet:
        logger.removeFilter(f)
    try:
        _raised, errors = _probe(caplog, fail=None)
    finally:
        for f in quiet:
            logger.addFilter(f)
    assert len(errors) == 1
    assert "ConcurrencySlotAcquisitionWithLeaseService" in errors[0].getMessage()


def test_a_server_failure_still_logs_and_raises(caplog):
    raised, errors = _probe(caplog, fail=httpx.ConnectError("connection refused"))
    assert not isinstance(raised, TimeoutError)
    assert len(errors) == 1
    assert "failed to process item" in errors[0].getMessage()

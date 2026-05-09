"""Tests for music_service.flows — task/flow wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# Prefect runs tasks and flows directly when called without a live server.
# Use the test harness so state writes go to an in-memory SQLite backend.
@pytest.fixture(autouse=True, scope="module")
def prefect_test_env():
    from prefect.testing.utilities import prefect_test_harness
    with prefect_test_harness():
        yield


def test_fetch_task_calls_ingest_run():
    from music_service.flows import fetch_task
    mock_pending = MagicMock()
    with patch("music_service.flows.ingest") as mock_ingest:
        mock_ingest.run.return_value = mock_pending
        result = fetch_task()
        mock_ingest.run.assert_called_once()
        assert result is mock_pending


def test_scan_task_calls_scan_run():
    from music_service.flows import scan_task
    mock_pending = MagicMock()
    with patch("music_service.flows.scan") as mock_scan:
        scan_task(mock_pending)
        mock_scan.run.assert_called_once_with(mock_pending)


def test_scan_task_with_no_pending():
    from music_service.flows import scan_task
    with patch("music_service.flows.scan") as mock_scan:
        scan_task(None)
        mock_scan.run.assert_called_once_with(None)


def test_reconcile_task_calls_reconcile_all():
    from music_service.flows import reconcile_task
    with patch("music_service.flows.reconcile") as mock_reconcile:
        reconcile_task()
        mock_reconcile.reconcile_all.assert_called_once()


def test_fetch_and_scan_flow_chains_fetch_then_scan_then_reconcile():
    from music_service.flows import fetch_and_scan_flow
    mock_pending = MagicMock()
    call_order: list[str] = []

    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.reconcile") as mock_reconcile:
        mock_ingest.run.side_effect = lambda: (call_order.append("fetch"), mock_pending)[1]
        mock_scan.run.side_effect = lambda p: call_order.append("scan")
        mock_reconcile.reconcile_all.side_effect = lambda: call_order.append("reconcile")

        fetch_and_scan_flow()

        assert call_order == ["fetch", "scan", "reconcile"]
        mock_scan.run.assert_called_once_with(mock_pending)


def test_scan_flow_skips_fetch():
    from music_service.flows import scan_flow
    with patch("music_service.flows.ingest") as mock_ingest, \
         patch("music_service.flows.scan") as mock_scan, \
         patch("music_service.flows.reconcile"):
        scan_flow()
        mock_ingest.run.assert_not_called()
        mock_scan.run.assert_called_once_with(None)

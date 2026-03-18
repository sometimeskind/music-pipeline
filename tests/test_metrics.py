"""Tests for pipeline.metrics — Prometheus text format output."""

import pytest

from pipeline.metrics import IngestMetrics, ScanMetrics, _gauge


# ---------------------------------------------------------------------------
# _gauge helper
# ---------------------------------------------------------------------------

def test_gauge_no_labels() -> None:
    result = _gauge("my_metric", 1)
    assert result == "# TYPE my_metric gauge\nmy_metric 1"


def test_gauge_with_labels() -> None:
    result = _gauge("my_metric", 1, {"reason": "auth_spotify"})
    assert 'reason="auth_spotify"' in result
    assert "my_metric{" in result


def test_gauge_float_value() -> None:
    result = _gauge("duration", 42.5)
    assert "42.5" in result


# ---------------------------------------------------------------------------
# ScanMetrics
# ---------------------------------------------------------------------------

def test_scan_metrics_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("pipeline.metrics._push", lambda body, job: pushed.append(body))

    m = ScanMetrics(success=True, duration_seconds=30, quarantined_tracks=2, navidrome_rescan_success=True)
    m.push()

    assert len(pushed) == 1
    body = pushed[0]
    assert "music_scan_last_run_success 1" in body
    assert "music_scan_duration_seconds 30" in body
    assert "music_scan_quarantined_tracks_total 2" in body
    assert "music_scan_navidrome_rescan_success 1" in body
    assert "failure_reason" not in body


def test_scan_metrics_failure_includes_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("pipeline.metrics._push", lambda body, job: pushed.append(body))

    m = ScanMetrics(success=False, failure_reason="disk_full")
    m.push()

    body = pushed[0]
    assert "music_scan_last_run_success 0" in body
    assert 'reason="disk_full"' in body


# ---------------------------------------------------------------------------
# IngestMetrics
# ---------------------------------------------------------------------------

def test_ingest_metrics_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("pipeline.metrics._push", lambda body, job: pushed.append(body))

    m = IngestMetrics(success=True, duration_seconds=120, playlists_total=3, playlists_skipped=1)
    m.push()

    body = pushed[0]
    assert "music_ingest_last_run_success 1" in body
    assert "music_ingest_duration_seconds 120" in body
    assert "music_ingest_playlists_total 3" in body
    assert "music_ingest_playlists_skipped_total 1" in body
    assert "failure_reason" not in body


def test_ingest_metrics_failure_includes_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("pipeline.metrics._push", lambda body, job: pushed.append(body))

    m = IngestMetrics(success=False, failure_reason="rate_limited")
    m.push()

    body = pushed[0]
    assert "music_ingest_last_run_success 0" in body
    assert 'reason="rate_limited"' in body


def test_push_job_label_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[str] = []
    monkeypatch.setattr("pipeline.metrics._push", lambda body, job: jobs.append(job))
    ScanMetrics().push()
    assert jobs == ["music_scan"]


def test_push_job_label_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[str] = []
    monkeypatch.setattr("pipeline.metrics._push", lambda body, job: jobs.append(job))
    IngestMetrics().push()
    assert jobs == ["music_ingest"]

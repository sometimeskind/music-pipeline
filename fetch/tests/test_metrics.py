"""Tests for pipeline.metrics — IngestMetrics Prometheus output."""

import pytest

from music_fetch.metrics import IngestMetrics, _gauge


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
# IngestMetrics
# ---------------------------------------------------------------------------

def test_ingest_metrics_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: calls.append((body, job)))

    m = IngestMetrics(success=True, duration_seconds=120, playlists_total=3, playlists_skipped=1)
    m.push()

    assert len(calls) == 1
    body, job = calls[0]
    assert job == "music_ingest"
    assert "music_ingest_last_run_success 1" in body
    assert "music_ingest_duration_seconds 120" in body
    assert "music_ingest_playlists_total 3" in body
    assert "music_ingest_playlists_skipped_total 1" in body
    assert "failure_reason" not in body


def test_ingest_metrics_failure_includes_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: calls.append((body, job)))

    m = IngestMetrics(success=False, failure_reason="rate_limited")
    m.push()

    assert len(calls) == 2
    body, job = calls[0]
    assert job == "music_ingest"
    assert "music_ingest_last_run_success 0" in body
    assert 'reason="rate_limited"' in body

    failure_body, failure_job = calls[1]
    assert failure_job == "music_ingest_failure"
    assert "music_ingest_failure_marker 1" in failure_body


def test_push_job_label_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[str] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: jobs.append(job))
    IngestMetrics().push()
    assert jobs == ["music_ingest"]


def test_failure_push_sends_two_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[str] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: jobs.append(job))
    IngestMetrics(success=False).push()
    assert jobs == ["music_ingest", "music_ingest_failure"]


def test_ingest_metrics_tracks_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushed.append(body))

    m = IngestMetrics(tracks_downloaded=42)
    m.push()

    assert "music_ingest_tracks_downloaded_total 42" in pushed[0]


def test_ingest_metrics_cookies_expired_true(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushed.append(body))

    m = IngestMetrics(cookies_expired=True)
    m.push()

    assert "music_ingest_cookies_expired 1" in pushed[0]


def test_ingest_metrics_cookies_expired_false_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[str] = []
    monkeypatch.setattr("music_fetch.metrics._push", lambda body, job: pushed.append(body))

    IngestMetrics().push()

    assert "music_ingest_cookies_expired 0" in pushed[0]

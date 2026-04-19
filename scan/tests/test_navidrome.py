"""Tests for pipeline.navidrome.trigger_scan()."""

from __future__ import annotations

import unittest.mock as mock

import pytest
import requests


def _make_subsonic_response(status: str = "ok") -> dict:
    return {"subsonic-response": {"status": status, "version": "1.8.0"}}


@pytest.fixture(autouse=True)
def clear_navidrome_env(monkeypatch):
    """Ensure Navidrome env vars are absent by default in every test."""
    monkeypatch.delenv("NAVIDROME_URL", raising=False)
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)


def test_no_url_skips_http_call():
    """When NAVIDROME_URL is unset, no HTTP request is made."""
    with mock.patch("music_scan.navidrome.requests.get") as mock_get:
        from music_scan.navidrome import trigger_scan
        trigger_scan()
    mock_get.assert_not_called()


def test_url_without_credentials_logs_warning(monkeypatch, caplog):
    """When URL is set but credentials are missing, log a warning and skip."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")

    with mock.patch("music_scan.navidrome.requests.get") as mock_get:
        import logging
        with caplog.at_level(logging.WARNING, logger="music_scan.navidrome"):
            from music_scan.navidrome import trigger_scan
            trigger_scan()

    mock_get.assert_not_called()
    assert "NAVIDROME_USER or NAVIDROME_PASSWORD is missing" in caplog.text


def test_url_with_only_user_logs_warning(monkeypatch, caplog):
    """When URL and user are set but password is missing, still warns."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")

    with mock.patch("music_scan.navidrome.requests.get") as mock_get:
        import logging
        with caplog.at_level(logging.WARNING, logger="music_scan.navidrome"):
            from music_scan.navidrome import trigger_scan
            trigger_scan()

    mock_get.assert_not_called()
    assert "NAVIDROME_USER or NAVIDROME_PASSWORD is missing" in caplog.text


def test_successful_scan_logs_info(monkeypatch, caplog):
    """Happy path: Subsonic returns ok → info logged, correct endpoint called."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    mock_resp = mock.Mock()
    mock_resp.json.return_value = _make_subsonic_response("ok")

    with mock.patch("music_scan.navidrome.requests.get", return_value=mock_resp) as mock_get:
        import logging
        with caplog.at_level(logging.INFO, logger="music_scan.navidrome"):
            from music_scan.navidrome import trigger_scan
            trigger_scan()

    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args
    assert call_kwargs[0][0] == "http://navidrome.example.com/rest/startScan.view"
    assert call_kwargs[1]["params"]["u"] == "admin"
    assert call_kwargs[1]["params"]["f"] == "json"
    assert "Navidrome library rescan triggered" in caplog.text


def test_trailing_slash_in_url_is_normalized(monkeypatch):
    """A trailing slash in NAVIDROME_URL must not produce a double slash."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com/")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    mock_resp = mock.Mock()
    mock_resp.json.return_value = _make_subsonic_response("ok")

    with mock.patch("music_scan.navidrome.requests.get", return_value=mock_resp) as mock_get:
        from music_scan.navidrome import trigger_scan
        trigger_scan()

    url_called = mock_get.call_args[0][0]
    assert "//" not in url_called.replace("http://", "")


def test_non_ok_subsonic_status_logs_warning(monkeypatch, caplog):
    """When Subsonic returns a non-ok status, a warning is logged."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    mock_resp = mock.Mock()
    mock_resp.json.return_value = _make_subsonic_response("failed")

    with mock.patch("music_scan.navidrome.requests.get", return_value=mock_resp):
        import logging
        with caplog.at_level(logging.WARNING, logger="music_scan.navidrome"):
            from music_scan.navidrome import trigger_scan
            trigger_scan()

    assert "non-ok status" in caplog.text


def test_http_error_logs_warning(monkeypatch, caplog):
    """When requests raises a RequestException, a warning is logged (not raised)."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    with mock.patch(
        "music_scan.navidrome.requests.get",
        side_effect=requests.ConnectionError("connection refused"),
    ):
        import logging
        with caplog.at_level(logging.WARNING, logger="music_scan.navidrome"):
            from music_scan.navidrome import trigger_scan
            trigger_scan()

    assert "Failed to trigger Navidrome rescan" in caplog.text

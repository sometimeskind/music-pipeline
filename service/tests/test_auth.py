"""Tests for music_service.auth — bearer-token authentication."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from music_service.api import create_app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("API_BEARER_TOKEN", "test-secret")
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def test_health_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_protected_without_token_returns_401(client):
    resp = client.post("/fetch/trigger")
    assert resp.status_code == 401


def test_protected_wrong_token_returns_401(client):
    resp = client.post("/fetch/trigger", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_protected_correct_token_returns_202(client):
    with patch("music_service.prefect_client.trigger_fetch", return_value=True):
        resp = client.post("/fetch/trigger", headers={"Authorization": "Bearer test-secret"})
    assert resp.status_code == 202

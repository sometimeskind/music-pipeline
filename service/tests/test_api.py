"""Tests for music_service.api — Flask routes."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_service.api import create_app


AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def orc():
    m = MagicMock()
    m.try_run_fetch.return_value = True
    m.try_run_scan.return_value = True
    return m


@pytest.fixture
def app(monkeypatch, tmp_path, orc):
    monkeypatch.setenv("API_BEARER_TOKEN", "test-secret")
    monkeypatch.setenv("MUSIC_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("MUSIC_QUARANTINE", str(tmp_path / "quarantine"))
    (tmp_path / "inbox").mkdir()
    (tmp_path / "quarantine").mkdir()
    flask_app = create_app(orc)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# GET /inbox
# ---------------------------------------------------------------------------


def test_inbox_list_empty(client):
    resp = client.get("/inbox", headers=AUTH)
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_inbox_list_returns_files(client, tmp_path):
    audio = tmp_path / "inbox" / "song.m4a"
    audio.write_bytes(b"audio")
    resp = client.get("/inbox", headers=AUTH)
    assert resp.status_code == 200
    files = resp.get_json()
    assert len(files) == 1
    assert files[0]["name"] == "song.m4a"
    assert files[0]["size"] == 5
    assert "modified" in files[0]


def test_inbox_list_nested(client, tmp_path):
    sub = tmp_path / "inbox" / "playlist"
    sub.mkdir()
    (sub / "track.mp3").write_bytes(b"x" * 100)
    resp = client.get("/inbox", headers=AUTH)
    files = resp.get_json()
    assert len(files) == 1
    assert files[0]["name"] == "playlist/track.mp3"
    assert files[0]["size"] == 100


# ---------------------------------------------------------------------------
# POST /inbox/upload
# ---------------------------------------------------------------------------


def test_inbox_upload_extracts_zip(client, tmp_path, orc):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("track.m4a", b"audio-data")
    buf.seek(0)

    resp = client.post("/inbox/upload", data=buf.read(), headers=AUTH, content_type="application/zip")
    assert resp.status_code == 200

    extracted = tmp_path / "inbox" / "track.m4a"
    assert extracted.exists()
    assert extracted.read_bytes() == b"audio-data"


def test_inbox_upload_triggers_debounced_scan(client, tmp_path, orc):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("track.m4a", b"x")
    buf.seek(0)

    client.post("/inbox/upload", data=buf.read(), headers=AUTH, content_type="application/zip")
    orc.schedule_scan.assert_called_once()


def test_inbox_upload_empty_body_returns_400(client):
    resp = client.post("/inbox/upload", data=b"", headers=AUTH, content_type="application/zip")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /quarantine
# ---------------------------------------------------------------------------


def test_quarantine_list_empty(client):
    resp = client.get("/quarantine", headers=AUTH)
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_quarantine_list_returns_files(client, tmp_path):
    f = tmp_path / "quarantine" / "bad.m4a"
    f.write_bytes(b"x" * 42)
    resp = client.get("/quarantine", headers=AUTH)
    files = resp.get_json()
    assert len(files) == 1
    assert files[0]["name"] == "bad.m4a"
    assert files[0]["size"] == 42


# ---------------------------------------------------------------------------
# GET /quarantine/download/<path>
# ---------------------------------------------------------------------------


def test_quarantine_download_file(client, tmp_path):
    f = tmp_path / "quarantine" / "bad.m4a"
    f.write_bytes(b"audio-content")
    resp = client.get("/quarantine/download/bad.m4a", headers=AUTH)
    assert resp.status_code == 200
    assert resp.data == b"audio-content"


def test_quarantine_download_directory_returns_zip(client, tmp_path):
    sub = tmp_path / "quarantine" / "album"
    sub.mkdir()
    (sub / "track1.m4a").write_bytes(b"t1")
    (sub / "track2.m4a").write_bytes(b"t2")

    resp = client.get("/quarantine/download/album", headers=AUTH)
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert "album/track1.m4a" in names
    assert "album/track2.m4a" in names


def test_quarantine_download_not_found(client):
    resp = client.get("/quarantine/download/nonexistent.m4a", headers=AUTH)
    assert resp.status_code == 404


def test_quarantine_download_path_traversal_blocked(client):
    resp = client.get("/quarantine/download/../../../etc/passwd", headers=AUTH)
    assert resp.status_code in (400, 403, 404)


# ---------------------------------------------------------------------------
# POST /fetch/trigger
# ---------------------------------------------------------------------------


def test_fetch_trigger_returns_202(client, orc):
    resp = client.post("/fetch/trigger", headers=AUTH)
    assert resp.status_code == 202
    orc.try_run_fetch.assert_called_once()


def test_fetch_trigger_returns_409_when_busy(client, orc):
    orc.try_run_fetch.return_value = False
    resp = client.post("/fetch/trigger", headers=AUTH)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /scan/trigger
# ---------------------------------------------------------------------------


def test_scan_trigger_returns_202(client, orc):
    resp = client.post("/scan/trigger", headers=AUTH)
    assert resp.status_code == 202
    orc.try_run_scan.assert_called_once()


def test_scan_trigger_returns_409_when_busy(client, orc):
    orc.try_run_scan.return_value = False
    resp = client.post("/scan/trigger", headers=AUTH)
    assert resp.status_code == 409

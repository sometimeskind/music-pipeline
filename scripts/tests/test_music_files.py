"""Tests for scripts/music-files CLI client."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# Load the script as a module (it has no .py extension)
# ---------------------------------------------------------------------------

SCRIPT_PATH = Path(__file__).parent.parent / "music-files"


@pytest.fixture(scope="session")
def mod():
    spec = importlib.util.spec_from_file_location("music_files", SCRIPT_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def default_env(monkeypatch):
    monkeypatch.setenv("MUSIC_PIPELINE_URL", "http://localhost:8080")
    monkeypatch.setenv("MUSIC_PIPELINE_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# _human_size
# ---------------------------------------------------------------------------


def test_human_size_bytes(mod):
    assert mod._human_size(0) == "0 B"
    assert mod._human_size(999) == "999 B"


def test_human_size_kb(mod):
    assert mod._human_size(1024) == "1.0 KB"
    assert mod._human_size(2048) == "2.0 KB"


def test_human_size_mb(mod):
    assert mod._human_size(1024 * 1024) == "1.0 MB"


def test_human_size_gb(mod):
    assert mod._human_size(1024 ** 3) == "1.0 GB"


# ---------------------------------------------------------------------------
# _print_table
# ---------------------------------------------------------------------------


def test_print_table_empty(mod, capsys):
    mod._print_table([])
    out = capsys.readouterr().out
    assert out == ""


def test_print_table_aligns_columns(mod, capsys):
    files = [
        {"name": "a.m4a", "size": 1024, "modified": "2024-01-01T00:00:00+00:00"},
        {"name": "long-name.m4a", "size": 2048 * 1024, "modified": "2024-06-15T12:30:00+00:00"},
    ]
    mod._print_table(files)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "a.m4a" in out
    assert "long-name.m4a" in out
    assert "1.0 KB" in out
    assert "2.0 MB" in out


# ---------------------------------------------------------------------------
# _url and _headers
# ---------------------------------------------------------------------------


def test_url_builds_correctly(mod):
    assert mod._url("/inbox") == "http://localhost:8080/inbox"


def test_url_no_double_slash(mod):
    result = mod._url("/health")
    assert "//" not in result.replace("http://", "")


def test_headers_contains_bearer(mod):
    h = mod._headers()
    assert h["Authorization"] == "Bearer test-token"


# ---------------------------------------------------------------------------
# list-inbox
# ---------------------------------------------------------------------------


def test_list_inbox_prints_table(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"name": "song.m4a", "size": 5120, "modified": "2024-03-01T10:00:00+00:00"}
    ]
    with patch("requests.get", return_value=mock_resp):
        mod.cmd_list_inbox()
    out = capsys.readouterr().out
    assert "song.m4a" in out
    assert "5.0 KB" in out


def test_list_inbox_empty(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []
    with patch("requests.get", return_value=mock_resp):
        mod.cmd_list_inbox()
    # Should not raise; empty output or empty table is fine


def test_list_inbox_http_error_exits(mod):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    with patch("requests.get", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_list_inbox()
    assert exc_info.value.code == 1


def test_list_inbox_connection_error_exits(mod):
    with patch("requests.get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_list_inbox()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# list-quarantine
# ---------------------------------------------------------------------------


def test_list_quarantine_prints_table(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"name": "bad.m4a", "size": 1024, "modified": "2024-04-01T00:00:00+00:00"}
    ]
    with patch("requests.get", return_value=mock_resp):
        mod.cmd_list_quarantine()
    out = capsys.readouterr().out
    assert "bad.m4a" in out


def test_list_quarantine_connection_error_exits(mod):
    with patch("requests.get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_list_quarantine()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_file(mod, tmp_path, capsys):
    audio = tmp_path / "track.m4a"
    audio.write_bytes(b"fake-audio-data")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        mod.cmd_upload(str(audio))

    args, kwargs = mock_post.call_args
    # The body should be valid zip bytes
    body = kwargs.get("data") or args[1]
    zf = zipfile.ZipFile(io.BytesIO(body))
    assert "track.m4a" in zf.namelist()

    out = capsys.readouterr().out
    assert out.strip()  # some confirmation message


def test_upload_directory(mod, tmp_path, capsys):
    d = tmp_path / "playlist"
    d.mkdir()
    (d / "a.m4a").write_bytes(b"aaa")
    (d / "b.m4a").write_bytes(b"bbb")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        mod.cmd_upload(str(d))

    args, kwargs = mock_post.call_args
    body = kwargs.get("data") or args[1]
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = zf.namelist()
    assert "a.m4a" in names
    assert "b.m4a" in names


def test_upload_http_error_exits(mod, tmp_path):
    audio = tmp_path / "track.m4a"
    audio.write_bytes(b"x")

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_upload(str(audio))
    assert exc_info.value.code == 1


def test_upload_nonexistent_path_exits(mod):
    with pytest.raises(SystemExit) as exc_info:
        mod.cmd_upload("/nonexistent/path/to/file.m4a")
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_plain_file_saved_to_cwd(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "audio/mp4"}
    mock_resp.content = b"audio-bytes"

    with patch("requests.get", return_value=mock_resp):
        mod.cmd_download("bad.m4a")

    assert (tmp_path / "bad.m4a").read_bytes() == b"audio-bytes"


def test_download_zip_extracted_in_cwd(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("album/track1.m4a", b"t1")
    buf.seek(0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/zip"}
    mock_resp.content = buf.read()

    with patch("requests.get", return_value=mock_resp):
        mod.cmd_download("album")

    assert (tmp_path / "album" / "track1.m4a").exists()


def test_download_http_error_exits(mod):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "not found"

    with patch("requests.get", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_download("missing.m4a")
    assert exc_info.value.code == 1


def test_download_connection_error_exits(mod):
    with patch("requests.get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_download("bad.m4a")
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# trigger-fetch
# ---------------------------------------------------------------------------


def test_trigger_fetch_accepted(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 202

    with patch("requests.post", return_value=mock_resp):
        mod.cmd_trigger_fetch()

    out = capsys.readouterr().out
    assert out == "Fetch started\n"


def test_trigger_fetch_busy(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 409

    with patch("requests.post", return_value=mock_resp):
        mod.cmd_trigger_fetch()

    out = capsys.readouterr().out
    assert out == "Pipeline busy\n"


def test_trigger_fetch_error_exits(mod):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server Error"

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_trigger_fetch()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# trigger-scan
# ---------------------------------------------------------------------------


def test_trigger_scan_accepted(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 202

    with patch("requests.post", return_value=mock_resp):
        mod.cmd_trigger_scan()

    out = capsys.readouterr().out
    assert out == "Scan started\n"


def test_trigger_scan_busy(mod, capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 409

    with patch("requests.post", return_value=mock_resp):
        mod.cmd_trigger_scan()

    out = capsys.readouterr().out
    assert out == "Pipeline busy\n"


def test_trigger_scan_error_exits(mod):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server Error"

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_trigger_scan()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Missing environment variables
# ---------------------------------------------------------------------------


def test_missing_url_exits(mod, monkeypatch):
    monkeypatch.delenv("MUSIC_PIPELINE_URL")
    with pytest.raises(SystemExit) as exc_info:
        mod.cmd_list_inbox()
    assert exc_info.value.code == 1


def test_missing_token_exits(mod, monkeypatch):
    monkeypatch.delenv("MUSIC_PIPELINE_TOKEN")
    with pytest.raises(SystemExit) as exc_info:
        mod.cmd_list_inbox()
    assert exc_info.value.code == 1

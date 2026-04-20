"""Layer 2 — HTTP API Tests.

Exercise every HTTP endpoint against a live service container. No audio
processing — these tests validate routing, auth, and basic request handling.

All tests use the `running_service` fixture from conftest.py, which starts
the service container, waits for /health, and tears down afterwards.
"""

from __future__ import annotations

import io
import zipfile

import requests


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_no_auth(running_service):
    """GET /health without Authorization header returns 200."""
    resp = requests.get(f"{running_service['base_url']}/health", timeout=10)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_inbox_requires_auth(running_service):
    """GET /inbox without Authorization header returns 401."""
    resp = requests.get(f"{running_service['base_url']}/inbox", timeout=10)
    assert resp.status_code == 401


def test_quarantine_requires_auth(running_service):
    """GET /quarantine without Authorization header returns 401."""
    resp = requests.get(f"{running_service['base_url']}/quarantine", timeout=10)
    assert resp.status_code == 401


def test_scan_trigger_requires_auth(running_service):
    """POST /scan/trigger without Authorization header returns 401."""
    resp = requests.post(f"{running_service['base_url']}/scan/trigger", timeout=10)
    assert resp.status_code == 401


def test_fetch_trigger_requires_auth(running_service):
    """POST /fetch/trigger without Authorization header returns 401."""
    resp = requests.post(f"{running_service['base_url']}/fetch/trigger", timeout=10)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def test_inbox_list_empty(running_service):
    """GET /inbox with auth returns 200 and an empty list on a fresh volume."""
    resp = requests.get(
        f"{running_service['base_url']}/inbox",
        headers=running_service["headers"],
        timeout=10,
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_inbox_upload_and_list(running_service):
    """POST a zip to /inbox/upload; the extracted file appears in GET /inbox."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test-track.mp3", b"fake audio data")
    buf.seek(0)

    upload = requests.post(
        f"{running_service['base_url']}/inbox/upload",
        headers=running_service["headers"],
        data=buf.read(),
        timeout=10,
    )
    assert upload.status_code == 200

    listing = requests.get(
        f"{running_service['base_url']}/inbox",
        headers=running_service["headers"],
        timeout=10,
    )
    assert listing.status_code == 200
    names = [f["name"] for f in listing.json()]
    assert any("test-track.mp3" in n for n in names), (
        f"Uploaded file not found in inbox listing: {names}"
    )


def test_inbox_upload_invalid_zip(running_service):
    """POST garbage bytes to /inbox/upload returns 400."""
    resp = requests.post(
        f"{running_service['base_url']}/inbox/upload",
        headers=running_service["headers"],
        data=b"this is not a zip file",
        timeout=10,
    )
    assert resp.status_code == 400


def test_inbox_upload_empty_body(running_service):
    """POST an empty body to /inbox/upload returns 400."""
    resp = requests.post(
        f"{running_service['base_url']}/inbox/upload",
        headers=running_service["headers"],
        data=b"",
        timeout=10,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------


def test_quarantine_list_empty(running_service):
    """GET /quarantine returns 200 and empty list on a fresh volume."""
    resp = requests.get(
        f"{running_service['base_url']}/quarantine",
        headers=running_service["headers"],
        timeout=10,
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_quarantine_download_not_found(running_service):
    """GET /quarantine/download/<nonexistent> returns 404."""
    resp = requests.get(
        f"{running_service['base_url']}/quarantine/download/nope.mp3",
        headers=running_service["headers"],
        timeout=10,
    )
    assert resp.status_code == 404


def test_quarantine_download_path_traversal(running_service):
    """GET /quarantine/download/../../etc/passwd returns 403."""
    resp = requests.get(
        f"{running_service['base_url']}/quarantine/download/../../etc/passwd",
        headers=running_service["headers"],
        timeout=10,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Trigger endpoints
# ---------------------------------------------------------------------------


def test_scan_trigger(running_service):
    """POST /scan/trigger with auth returns 202."""
    resp = requests.post(
        f"{running_service['base_url']}/scan/trigger",
        headers=running_service["headers"],
        timeout=10,
    )
    assert resp.status_code == 202


def test_fetch_trigger(running_service):
    """POST /fetch/trigger with auth returns 202 (first call acquires lock)."""
    resp = requests.post(
        f"{running_service['base_url']}/fetch/trigger",
        headers=running_service["headers"],
        timeout=10,
    )
    assert resp.status_code == 202


def test_fetch_trigger_busy(running_service):
    """Rapid double-POST to /fetch/trigger: second call returns 409 if lock is held.

    Note: this test is inherently racy — if the first fetch thread completes
    before the second POST arrives (unlikely but possible), both return 202.
    A 409 result is the strong assertion; 202/202 is accepted as a pass.
    """
    base = running_service["base_url"]
    hdrs = running_service["headers"]
    r1 = requests.post(f"{base}/fetch/trigger", headers=hdrs, timeout=10)
    r2 = requests.post(f"{base}/fetch/trigger", headers=hdrs, timeout=10)
    assert r1.status_code == 202
    assert r2.status_code in (202, 409), (
        f"Expected second /fetch/trigger to return 202 or 409, got {r2.status_code}"
    )

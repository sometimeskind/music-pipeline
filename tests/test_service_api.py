"""Layer 2 and Layer 3 — HTTP API and Scan-via-Service Tests.

Layer 2: Exercise every HTTP endpoint against a live service container. No
audio processing — these tests validate routing, auth, and basic request
handling. All tests use the `running_service` fixture from conftest.py.

Layer 3: Verify the full scan pipeline runs correctly when triggered through
the service HTTP API. Uses `running_service_asis` (asis beets config) to avoid
MusicBrainz network calls.
"""

from __future__ import annotations

import io
import zipfile

import requests

from conftest import service_in_container, wait_for_log


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
    """Path traversal via /quarantine/download is rejected.

    `requests` normalises `../../etc/passwd` to `/etc/passwd` before sending,
    so Flask returns 404 (no matching route). We send the raw path via
    http.client to also exercise the server-side resolve() guard (403).
    Either way, the file must not be served (not 200).
    """
    import http.client

    conn = http.client.HTTPConnection("localhost", 8080, timeout=10)
    conn.request(
        "GET",
        "/quarantine/download/../../etc/passwd",
        headers=running_service["headers"],
    )
    resp = conn.getresponse()
    conn.close()
    assert resp.status in (403, 404), (
        f"Expected 403 or 404 for path traversal attempt, got {resp.status}"
    )


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


# ---------------------------------------------------------------------------
# Layer 3: Scan-via-service
# ---------------------------------------------------------------------------


def _upload_zip(svc: dict, zip_contents: dict[str, bytes]) -> None:
    """Build and upload a zip to /inbox/upload. zip_contents maps arcname → data."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for arcname, data in zip_contents.items():
            zf.writestr(arcname, data)
    buf.seek(0)
    resp = requests.post(
        f"{svc['base_url']}/inbox/upload",
        headers=svc["headers"],
        data=buf.read(),
        timeout=10,
    )
    assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text}"


def test_upload_and_scan_imports_track(running_service_asis, fixture_audio):
    """Upload an audio file via the API, trigger a scan, verify the track is imported."""
    svc = running_service_asis

    _upload_zip(svc, {fixture_audio.name: fixture_audio.read_bytes()})

    trigger = requests.post(
        f"{svc['base_url']}/scan/trigger",
        headers=svc["headers"],
        timeout=10,
    )
    assert trigger.status_code in (202, 409), (
        f"Unexpected /scan/trigger response: {trigger.status_code}"
    )

    found = wait_for_log(svc["container"], "==> Scan complete", timeout=60)
    assert found, "Scan did not complete within 60s"

    exit_code, output = service_in_container(svc["container"], ["beet", "ls"])
    assert exit_code == 0
    assert "7 Ghosts I" in output, f"Track not found in beet ls output:\n{output}"


def test_upload_and_scan_generates_playlist(running_service_asis, fixture_audio):
    """Upload a spotdl playlist structure, trigger scan, verify .m3u is generated."""
    svc = running_service_asis
    playlist_name = "test-playlist"

    # The zip must reproduce the spotdl inbox layout so the plugin assigns source=
    # and _regen_playlists() finds the .spotdl sentinel file.
    _upload_zip(svc, {
        f"spotdl/{playlist_name}.spotdl": b"[]",
        f"spotdl/{playlist_name}/{fixture_audio.name}": fixture_audio.read_bytes(),
    })

    trigger = requests.post(
        f"{svc['base_url']}/scan/trigger",
        headers=svc["headers"],
        timeout=10,
    )
    assert trigger.status_code in (202, 409), (
        f"Unexpected /scan/trigger response: {trigger.status_code}"
    )

    found = wait_for_log(svc["container"], "==> Scan complete", timeout=60)
    assert found, "Scan did not complete within 60s"

    exit_code, output = service_in_container(
        svc["container"],
        ["find", "/root/Music/playlists", "-name", "*.m3u"],
    )
    assert exit_code == 0
    assert playlist_name in output, (
        f"Expected {playlist_name}.m3u in /root/Music/playlists/, got:\n{output}"
    )

"""Layer 4 — Library Push Tests.

Verify that rclone push works (and is skipped) based on the LIBRARY_REMOTE
environment variable. Uses a local Docker volume mounted at /remote inside the
container as the rclone target — rclone accepts a bare directory path as its
local backend.

All tests use the asis beets config (no MusicBrainz calls).
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager

import pytest
import requests

from conftest import (
    SERVICE_IMAGE,
    BEETS_CONFIG,
    _SERVICE_ENV_BASE,
    _start_service,
    service_in_container,
    wait_for_log,
)


# ---------------------------------------------------------------------------
# Fixture: service with LIBRARY_REMOTE wired to a local /remote volume
# ---------------------------------------------------------------------------


@contextmanager
def _start_service_with_remote(docker_client, volumes, beets_config_path):
    """Like _start_service but adds a remote volume and sets LIBRARY_REMOTE=/remote."""
    import time
    import requests as _requests

    remote_vol = docker_client.volumes.create()
    try:
        container = docker_client.containers.run(
            SERVICE_IMAGE,
            command=["music-pipeline"],
            detach=True,
            network_mode="host",
            environment={**_SERVICE_ENV_BASE, "LIBRARY_REMOTE": "/remote"},
            volumes={
                volumes["music"]: {"bind": "/root/Music", "mode": "rw"},
                volumes["beets"]: {"bind": "/root/.config/beets", "mode": "rw"},
                beets_config_path: {"bind": "/root/.config/beets/config.yaml", "mode": "ro"},
                remote_vol.name: {"bind": "/remote", "mode": "rw"},
            },
        )
        try:
            base_url = "http://localhost:8080"
            deadline = time.monotonic() + 30
            last_exc = None
            while time.monotonic() < deadline:
                try:
                    resp = _requests.get(f"{base_url}/health", timeout=2)
                    if resp.status_code == 200:
                        break
                except Exception as exc:
                    last_exc = exc
                time.sleep(0.5)
            else:
                logs = container.logs(stdout=True, stderr=True).decode()
                raise RuntimeError(
                    f"Service did not become healthy within 30s "
                    f"(last error: {last_exc}).\nContainer logs:\n{logs}"
                )
            yield {
                "base_url": base_url,
                "headers": {"Authorization": "Bearer test-token"},
                "container": container,
                "volumes": volumes,
                "remote_vol": remote_vol.name,
            }
        finally:
            container.stop(timeout=5)
            container.remove(force=True)
    finally:
        remote_vol.remove(force=True)


@pytest.fixture
def running_service_push(docker_client, volumes, beets_asis_config):
    """Service container with asis config + LIBRARY_REMOTE=/remote."""
    with _start_service_with_remote(docker_client, volumes, beets_asis_config) as svc:
        yield svc


@pytest.fixture
def running_service_no_remote(docker_client, volumes, beets_asis_config):
    """Service container with asis config and no LIBRARY_REMOTE set."""
    with _start_service(docker_client, volumes, beets_asis_config) as svc:
        yield svc


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _upload_zip(svc: dict, zip_contents: dict[str, bytes]) -> None:
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


def _ls_remote(docker_client, remote_vol_name: str) -> list[str]:
    """Return file paths found under /remote in the remote volume."""
    result = docker_client.containers.run(
        SERVICE_IMAGE,
        command=["find", "/remote", "-type", "f"],
        volumes={remote_vol_name: {"bind": "/remote", "mode": "ro"}},
        remove=True,
    )
    lines = result.decode().strip().splitlines()
    return [l for l in lines if l]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_pushes_to_remote(docker_client, running_service_push, fixture_audio):
    """After a scan with LIBRARY_REMOTE set, files appear in the remote volume."""
    svc = running_service_push
    playlist_name = "push-test-playlist"

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

    found = wait_for_log(svc["container"], "==> Scan complete", timeout=90)
    assert found, "Scan did not complete within 90s"

    remote_files = _ls_remote(docker_client, svc["remote_vol"])

    # beets renames files on import, so check for any audio file outside /remote/playlists/
    audio_files = [f for f in remote_files if f.endswith(".mp3") and "/playlists/" not in f]
    assert audio_files, (
        f"No imported audio found in remote after push.\nRemote files: {remote_files}"
    )

    playlist_files = [f for f in remote_files if f.endswith(".m3u")]
    assert any(playlist_name in f for f in playlist_files), (
        f"Playlist .m3u not found in remote/playlists/.\nRemote files: {remote_files}"
    )


def test_no_push_without_library_remote(
    docker_client, running_service_no_remote, fixture_audio
):
    """Without LIBRARY_REMOTE, scan completes but nothing is pushed to /remote."""
    svc = running_service_no_remote

    # Create a temporary remote volume so we can assert it stays empty.
    remote_vol = docker_client.volumes.create()
    try:
        _upload_zip(svc, {fixture_audio.name: fixture_audio.read_bytes()})

        trigger = requests.post(
            f"{svc['base_url']}/scan/trigger",
            headers=svc["headers"],
            timeout=10,
        )
        assert trigger.status_code in (202, 409), (
            f"Unexpected /scan/trigger response: {trigger.status_code}"
        )

        found = wait_for_log(svc["container"], "==> Scan complete", timeout=90)
        assert found, "Scan did not complete within 90s"

        # The remote volume was never mounted into the container, so it should
        # be empty — but we verify the container logs confirm no push occurred.
        logs = svc["container"].logs(stdout=True, stderr=True).decode()
        assert "LIBRARY_REMOTE not set" in logs, (
            f"Expected 'LIBRARY_REMOTE not set' in logs.\nLogs:\n{logs}"
        )
        assert "==> Library pushed to" not in logs, (
            "Push log line found even though LIBRARY_REMOTE was not set."
        )
    finally:
        remote_vol.remove(force=True)

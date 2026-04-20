"""Layer 5 — Full End-to-End Integration Tests (auth-required, local-only).

Exercises the full pipeline via the unified service API:
  POST /fetch/trigger → spotdl downloads → beets import → library push.

Prerequisites:
- SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set (via `op run --env-file .env.tpl`)
- cookies.txt must be present at the repo root
- TEST_PLAYLIST_URL must be set to a small Spotify playlist URL (≤10 tracks)
  e.g. export TEST_PLAYLIST_URL=https://open.spotify.com/playlist/<id>

Run locally with:
    just test-auth
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from conftest import (
    SERVICE_IMAGE,
    BEETS_CONFIG,
    COOKIES_PATH,
    SPOTDL_CONFIG,
    service_in_container,
    wait_for_log,
)

pytestmark = pytest.mark.auth

_TEST_PLAYLIST_URL = os.environ.get("TEST_PLAYLIST_URL", "")
_TEST_PLAYLIST_NAME = "test-e2e-small"

_MISSING_PREREQS = []
if not os.environ.get("SPOTIFY_CLIENT_ID"):
    _MISSING_PREREQS.append("SPOTIFY_CLIENT_ID not set")
if not os.environ.get("SPOTIFY_CLIENT_SECRET"):
    _MISSING_PREREQS.append("SPOTIFY_CLIENT_SECRET not set")
if not COOKIES_PATH.exists():
    _MISSING_PREREQS.append(f"cookies.txt not found at {COOKIES_PATH}")
if not _TEST_PLAYLIST_URL:
    _MISSING_PREREQS.append("TEST_PLAYLIST_URL not set")

_SKIP_REASON = "; ".join(_MISSING_PREREQS) + " — run via `just test-auth`"


@contextmanager
def _start_service_e2e(docker_client, volumes, beets_config_path: str, playlists_conf_path: str):
    """Start the service with real Spotify creds, cookies.txt, and LIBRARY_REMOTE=/remote.

    Yields a dict with base_url, headers, container, volumes, and remote_vol.
    """
    import time
    import requests as _requests

    remote_vol = docker_client.volumes.create()
    try:
        container = docker_client.containers.run(
            SERVICE_IMAGE,
            command=["music-pipeline"],
            detach=True,
            network_mode="host",
            environment={
                "API_BEARER_TOKEN": "test-token",
                "SPOTIFY_CLIENT_ID": os.environ["SPOTIFY_CLIENT_ID"],
                "SPOTIFY_CLIENT_SECRET": os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
                "LIBRARY_REMOTE": "/remote",
            },
            volumes={
                volumes["music"]: {"bind": "/root/Music", "mode": "rw"},
                volumes["beets"]: {"bind": "/root/.config/beets", "mode": "rw"},
                beets_config_path: {"bind": "/root/.config/beets/config.yaml", "mode": "ro"},
                SPOTDL_CONFIG: {"bind": "/root/.config/spotdl/config.json", "mode": "ro"},
                str(COOKIES_PATH): {"bind": "/root/.config/spotdl/cookies.txt", "mode": "ro"},
                playlists_conf_path: {
                    "bind": "/root/.config/music-pipeline/playlists.conf",
                    "mode": "ro",
                },
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
            container.stop(timeout=10)
            container.remove(force=True)
    finally:
        remote_vol.remove(force=True)


def _ls_remote(docker_client, remote_vol_name: str) -> list[str]:
    """Return file paths found under /remote in the remote volume."""
    result = docker_client.containers.run(
        SERVICE_IMAGE,
        command=["find", "/remote", "-type", "f"],
        volumes={remote_vol_name: {"bind": "/remote", "mode": "ro"}},
        remove=True,
    )
    lines = result.decode().strip().splitlines()
    return [line for line in lines if line]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(bool(_MISSING_PREREQS), reason=_SKIP_REASON)
def test_full_pipeline_via_api(docker_client, volumes, beets_asis_config):
    """Full pipeline via API: fetch trigger → spotdl download → beets import → push.

    Uses asis beets config to avoid MusicBrainz network calls. Verifies:
    - Tracks downloaded to inbox after /fetch/trigger
    - Tracks imported to the beets library with the correct source= tag
    - .m3u playlist generated in /root/Music/playlists/
    - Audio and playlist files pushed to LIBRARY_REMOTE (/remote)
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".conf", delete=False, prefix="test-e2e-playlists-"
    ) as f:
        f.write(f"{_TEST_PLAYLIST_NAME}  {_TEST_PLAYLIST_URL}\n")
        test_conf = f.name

    try:
        with _start_service_e2e(docker_client, volumes, beets_asis_config, test_conf) as svc:
            # Trigger fetch — orchestrator chains into scan on completion
            resp = requests.post(
                f"{svc['base_url']}/fetch/trigger",
                headers=svc["headers"],
                timeout=10,
            )
            assert resp.status_code == 202, (
                f"POST /fetch/trigger returned {resp.status_code}: {resp.text}"
            )

            # Wait for the full pipeline (fetch + chained scan); spotdl is slow
            found = wait_for_log(svc["container"], "==> Scan complete", timeout=600)
            if not found:
                logs = svc["container"].logs(stdout=True, stderr=True).decode()
                pytest.fail(f"Pipeline did not complete within 600s.\nLogs:\n{logs}")

            # 1. Inbox should contain downloaded files
            _, inbox_listing = service_in_container(
                svc["container"],
                ["find", f"/root/Music/inbox/spotdl/{_TEST_PLAYLIST_NAME}", "-type", "f"],
            )
            if not inbox_listing.strip():
                logs = svc["container"].logs(stdout=True, stderr=True).decode()
                pytest.fail(
                    f"No files downloaded to inbox for {_TEST_PLAYLIST_NAME}.\n"
                    f"Container logs:\n{logs}"
                )

            # 2. Tracks must be tagged with the correct source= in the beets library
            _, beet_output = service_in_container(
                svc["container"], ["beet", "ls", f"source:{_TEST_PLAYLIST_NAME}"]
            )
            assert beet_output.strip(), (
                f"No tracks tagged source={_TEST_PLAYLIST_NAME} after scan.\n"
                f"beet ls output: {beet_output}"
            )

            # 3. .m3u playlist must exist and be non-empty
            _, m3u_content = service_in_container(
                svc["container"],
                ["cat", f"/root/Music/playlists/{_TEST_PLAYLIST_NAME}.m3u"],
            )
            assert m3u_content.strip(), (
                f"{_TEST_PLAYLIST_NAME}.m3u is empty or missing.\nContent: {m3u_content}"
            )

            # 4. Remote volume must contain pushed audio and the .m3u
            remote_files = _ls_remote(docker_client, svc["remote_vol"])
            audio_files = [
                f for f in remote_files
                if f.endswith((".mp3", ".m4a", ".flac", ".ogg")) and "/playlists/" not in f
            ]
            assert audio_files, (
                f"No audio files pushed to remote.\nRemote files: {remote_files}"
            )
            playlist_files = [f for f in remote_files if f.endswith(".m3u")]
            assert any(_TEST_PLAYLIST_NAME in f for f in playlist_files), (
                f"Playlist .m3u not pushed to remote.\nRemote files: {remote_files}"
            )
    finally:
        Path(test_conf).unlink(missing_ok=True)

"""Layer 1 — Service Smoke Tests.

Verify that the unified service image starts correctly, all entry points are
installed, system dependencies are present, and the chroma plugin loads.

These tests require no audio fixtures and run quickly. The image under test is
controlled by the SERVICE_IMAGE environment variable.
"""

from __future__ import annotations

import requests

from conftest import SERVICE_IMAGE, running_service, service_in_container  # noqa: F401


# ---------------------------------------------------------------------------
# Layer 1a — Health check (service starts and API is reachable)
# ---------------------------------------------------------------------------


def test_service_health(running_service):
    """Service container starts, Flask/waitress initialise, /health returns 200."""
    resp = requests.get(f"{running_service['base_url']}/health", timeout=10)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Layer 1b — Entry points
# ---------------------------------------------------------------------------


def test_entry_point_music_pipeline(docker_client):
    """music-pipeline --help exits 0 (entry point is installed)."""
    # music-pipeline validates env vars before starting waitress, so --help
    # won't work — instead we check the entry point exists via which.
    exit_code, output = _run_once(docker_client, ["which", "music-pipeline"])
    assert exit_code == 0, f"music-pipeline not found on PATH. Output:\n{output}"


def test_entry_point_music_ingest(docker_client):
    """music-ingest entry point is installed on PATH."""
    exit_code, output = _run_once(docker_client, ["which", "music-ingest"])
    assert exit_code == 0, f"music-ingest not found on PATH. Output:\n{output}"


def test_entry_point_music_scan(docker_client):
    """music-scan entry point is installed on PATH."""
    exit_code, output = _run_once(docker_client, ["which", "music-scan"])
    assert exit_code == 0, f"music-scan not found on PATH. Output:\n{output}"


# ---------------------------------------------------------------------------
# Layer 1c — System dependencies
# ---------------------------------------------------------------------------


def test_rclone_installed(docker_client):
    """rclone is present and reports a version string."""
    exit_code, output = _run_once(docker_client, ["rclone", "version"])
    assert exit_code == 0, f"rclone version failed. Output:\n{output}"
    assert "rclone" in output.lower()


def test_fpcalc_installed(docker_client):
    """fpcalc (libchromaprint-tools) is present and functional."""
    exit_code, output = _run_once(docker_client, ["fpcalc", "-version"])
    assert exit_code == 0, f"fpcalc -version failed. Output:\n{output}"
    assert "fpcalc" in output.lower()


def test_ffmpeg_installed(docker_client):
    """ffmpeg is present."""
    exit_code, output = _run_once(docker_client, ["ffmpeg", "-version"])
    assert exit_code == 0, f"ffmpeg -version failed. Output:\n{output}"
    assert "ffmpeg" in output.lower()


def test_node_installed(docker_client):
    """node is present (required by spotdl/yt-dlp JS runtime)."""
    exit_code, output = _run_once(docker_client, ["node", "--version"])
    assert exit_code == 0, f"node --version failed. Output:\n{output}"


# ---------------------------------------------------------------------------
# Layer 1d — Beets chroma plugin
# ---------------------------------------------------------------------------


def test_beet_chroma_plugin(docker_client, volumes):
    """beet version lists the chroma plugin against the service image."""
    from conftest import BEETS_CONFIG

    c = docker_client.containers.create(
        SERVICE_IMAGE,
        entrypoint=["beet"],
        command=["version"],
        volumes={
            volumes["music"]: {"bind": "/root/Music", "mode": "rw"},
            volumes["beets"]: {"bind": "/root/.config/beets", "mode": "rw"},
            BEETS_CONFIG: {"bind": "/root/.config/beets/config.yaml", "mode": "ro"},
        },
    )
    c.start()
    c.wait()
    output = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    assert "chroma" in output, (
        "chroma plugin not listed in `beet version` output against service image.\n"
        f"Full output:\n{output}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_once(docker_client, command: list[str]) -> tuple[int, str]:
    """Run a one-shot command in the service image and return (exit_code, output)."""
    c = docker_client.containers.create(SERVICE_IMAGE, command=command)
    c.start()
    exit_code = c.wait()["StatusCode"]
    output = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    return exit_code, output

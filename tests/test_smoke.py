"""Scenario 1 — Smoke Test and Scenario 1a — Chroma Plugin Verification.

These tests require no audio fixtures and run quickly. They verify that the
scan container image starts, runs, and has all required dependencies intact.
"""

from __future__ import annotations

import pytest

from conftest import (
    BEETS_CONFIG,
    SCAN_IMAGE,
    run_scan,
    scan_binds,
)


# ---------------------------------------------------------------------------
# Scenario 1 — Smoke Test
# ---------------------------------------------------------------------------


def test_smoke_empty_inbox(docker_client, volumes):
    """Scan against empty volumes exits 0 and reaches the completion log line."""
    exit_code, logs = run_scan(docker_client, volumes)
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"
    assert "music-scan complete" in logs, (
        "Expected 'music-scan complete' in logs. Logs:\n" + logs
    )


# ---------------------------------------------------------------------------
# Scenario 1a — Chroma Plugin Verification
# ---------------------------------------------------------------------------


def test_fpcalc_installed(docker_client):
    """fpcalc binary (from libchromaprint-tools) is present and functional."""
    result = docker_client.containers.run(
        SCAN_IMAGE,
        command=["fpcalc", "-version"],
        remove=True,
    )
    output = result.decode().lower()
    assert "fpcalc" in output, f"Unexpected fpcalc output: {output!r}"


def test_acoustid_importable(docker_client):
    """pyacoustid is importable and reports a version string."""
    result = docker_client.containers.run(
        SCAN_IMAGE,
        command=["python", "-c", "import acoustid; print(acoustid.__version__)"],
        remove=True,
    )
    version = result.decode().strip()
    assert version, "acoustid.__version__ returned an empty string"


def test_beet_version_includes_chroma(docker_client, volumes):
    """beet version lists the chroma plugin — confirms it loaded without errors."""
    c = docker_client.containers.create(
        SCAN_IMAGE,
        entrypoint=["beet"],
        command=["version"],
        volumes=scan_binds(volumes),
    )
    c.start()
    c.wait()
    output = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    assert "chroma" in output, (
        "chroma plugin not listed in `beet version` output.\n"
        f"Full output:\n{output}"
    )

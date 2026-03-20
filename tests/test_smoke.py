"""Scenario 1 — Smoke Test and Scenario 1a — Chroma Plugin Verification.

These tests require no audio fixtures and run quickly. They verify that the
scan container image starts, runs, and has all required dependencies intact.
"""

from __future__ import annotations

import pytest

from conftest import (
    BEETS_CONFIG,
    SCAN_IMAGE,
    beet_import_verbose,
    ls_in_volume,
    put_file,
    run_scan,
    scan_binds,
    scan_binds_test,
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
    """pyacoustid is importable and exposes the fingerprint function."""
    docker_client.containers.run(
        SCAN_IMAGE,
        command=["python", "-c", "import acoustid; assert hasattr(acoustid, 'fingerprint')"],
        remove=True,
    )


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


# ---------------------------------------------------------------------------
# Scenario 1c — Recording ID Lookup (Simulated AcoustID Path)
# ---------------------------------------------------------------------------
#
# AcoustID fingerprinting yields a MusicBrainz recording ID, which beets then
# looks up directly. This test simulates that path via `beet import --search-id`
# — bypassing both text search and AcoustID while exercising the recording-ID →
# MusicBrainz lookup → import code path that chroma enables in production.

_RECORDING_MBID = "1d1bb32a-5bc6-4b6f-88cc-c043f6c52509"  # "7 Ghosts I" by NIN


def test_search_id_imports_track_to_library(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """Track imported via --search-id lands in the library (recording-ID path)."""
    put_file(docker_client, volumes, "/root/Music/inbox", fixture_audio)

    verbose = beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config,
        extra_flags=["--search-id", _RECORDING_MBID],
    )

    library_files = ls_in_volume(docker_client, volumes, "/root/Music/library")
    assert library_files, (
        "No files found in /root/Music/library after --search-id import.\n"
        f"beet -vv import output:\n{verbose}"
    )


def test_search_id_import_then_scan_succeeds(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """Full scan pipeline succeeds when the track was imported via recording ID."""
    put_file(docker_client, volumes, "/root/Music/inbox", fixture_audio)

    beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config,
        extra_flags=["--search-id", _RECORDING_MBID],
    )
    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

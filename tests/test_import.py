"""Scenario 2 — Manual File Drop, Scenario 3 — Playlist Import, Scenario 4 — Duplicate Handling.

These tests require `tests/fixtures/audio/track-a.m4a` — a CC-licensed audio file
indexed in MusicBrainz. See tests/fixtures/audio/README.md for how to source one.
All tests in this module are skipped if that file is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    SCAN_IMAGE,
    beet_ls,
    cat_in_volume,
    ls_in_volume,
    mkdir_in_volume,
    put_bytes,
    put_file,
    run_scan,
    scan_binds,
)

TRACK_A = Path(__file__).parent / "fixtures" / "audio" / "track-a.m4a"

pytestmark = pytest.mark.skipif(
    not TRACK_A.exists(),
    reason=(
        f"track-a.m4a not found at {TRACK_A}. "
        "See tests/fixtures/audio/README.md for how to source a CC-licensed test file."
    ),
)


# ---------------------------------------------------------------------------
# Scenario 2 — Manual File Drop into Inbox
# ---------------------------------------------------------------------------


def test_file_drop_known_track_imported_to_library(docker_client, volumes):
    """Case A: a well-known track lands in the library, not the quarantine."""
    put_file(docker_client, volumes, "/root/Music/inbox", TRACK_A)

    exit_code, logs = run_scan(docker_client, volumes)
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    library_files = ls_in_volume(docker_client, volumes, "/root/Music/library")
    assert library_files, (
        "No files found in /root/Music/library after import.\n"
        "The track may have gone to quarantine — check if the MusicBrainz match "
        "confidence was too low (raise strong_rec_thresh in config/beets/config.yaml).\n"
        f"Scan logs:\n{logs}"
    )


def test_file_drop_inbox_cleared_after_import(docker_client, volumes):
    """After import, no audio files remain loose in the inbox root."""
    put_file(docker_client, volumes, "/root/Music/inbox", TRACK_A)
    run_scan(docker_client, volumes)

    # Only check inbox root (maxdepth 1); spotdl/ subdir is fine to have files
    result = docker_client.containers.run(
        SCAN_IMAGE,
        command=[
            "find", "/root/Music/inbox", "-maxdepth", "1",
            "-type", "f", "-name", "*.m4a",
        ],
        volumes={volumes["music"]: {"bind": "/root/Music", "mode": "ro"}},
        remove=True,
    )
    leftover = result.decode().strip()
    assert not leftover, (
        f"Audio files still in inbox root after import: {leftover}"
    )


def test_file_drop_noise_goes_to_quarantine(docker_client, volumes):
    """Case B: a noise file that won't match MusicBrainz is moved to quarantine."""
    # Generate a short noise file inside the container using ffmpeg
    docker_client.containers.run(
        SCAN_IMAGE,
        command=[
            "ffmpeg", "-f", "lavfi", "-i", "anoisesrc=d=5",
            "-ar", "44100", "-y", "/root/Music/inbox/noise.mp3",
        ],
        volumes=scan_binds(volumes),
        remove=True,
    )

    exit_code, logs = run_scan(docker_client, volumes)
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    quarantine_files = ls_in_volume(docker_client, volumes, "/root/Music/quarantine")
    assert any("noise" in f for f in quarantine_files), (
        f"noise.mp3 not found in quarantine. Quarantine contents: {quarantine_files}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Simulated spotdl Playlist Import
# ---------------------------------------------------------------------------


def test_playlist_import_source_tag_applied(docker_client, volumes):
    """Track imported from a spotdl playlist dir is tagged source=<playlist-name>."""
    _setup_playlist(docker_client, volumes, "test-playlist")

    exit_code, logs = run_scan(docker_client, volumes)
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    output = beet_ls(docker_client, volumes, "source:test-playlist")
    assert output.strip(), (
        "No tracks found with source=test-playlist in the beets library.\n"
        f"Scan logs:\n{logs}"
    )


def test_playlist_import_m3u_generated(docker_client, volumes):
    """After scan, a .m3u file exists for the playlist with a relative library path."""
    _setup_playlist(docker_client, volumes, "test-playlist")
    run_scan(docker_client, volumes)

    m3u = cat_in_volume(docker_client, volumes, "/root/Music/playlists/test-playlist.m3u")
    assert m3u.strip(), "test-playlist.m3u is empty"
    # Paths must be relative (for portability)
    assert not any(line.startswith("/") for line in m3u.splitlines() if line), (
        f"Expected relative paths in .m3u, got absolute:\n{m3u}"
    )
    assert "../library/" in m3u, (
        f"Expected paths traversing to sibling library/ dir, got:\n{m3u}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Duplicate Handling
# ---------------------------------------------------------------------------


def test_duplicate_import_skipped(docker_client, volumes):
    """Re-importing an already-present track does not create a second beets entry."""
    _setup_playlist(docker_client, volumes, "test-playlist")
    run_scan(docker_client, volumes)

    # Import the same track again via a second scan
    _setup_playlist(docker_client, volumes, "test-playlist")
    exit_code, logs = run_scan(docker_client, volumes)
    assert exit_code == 0, f"Second scan exited {exit_code}. Logs:\n{logs}"

    output = beet_ls(docker_client, volumes, "source:test-playlist")
    entries = [line for line in output.strip().splitlines() if line]
    assert len(entries) == 1, (
        f"Expected exactly 1 beets entry for test-playlist, found {len(entries)}:\n"
        + "\n".join(entries)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_playlist(client, vol_names: dict, playlist_name: str) -> None:
    """Create a fake spotdl playlist directory with track-a and a .spotdl state file."""
    playlist_dir = f"/root/Music/inbox/spotdl/{playlist_name}"
    mkdir_in_volume(client, vol_names, playlist_dir)
    put_file(client, vol_names, playlist_dir, TRACK_A)
    put_bytes(
        client,
        vol_names,
        f"/root/Music/inbox/spotdl/{playlist_name}.spotdl",
        json.dumps({"type": "sync", "query": [], "songs": []}).encode(),
    )

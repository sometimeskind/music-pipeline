"""Scenario 5 — Full Ingest with Spotify (local-only).

These tests require Spotify credentials and cookies.txt. They are marked with
`auth` and excluded from CI. Run locally with:

    just test-auth

Prerequisites:
- SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set (via `op run --env-file .env.tpl`)
- cookies.txt must be present at the repo root
- TEST_PLAYLIST_URL must be set to a small Spotify playlist URL (≤10 tracks)
  e.g. export TEST_PLAYLIST_URL=https://open.spotify.com/playlist/<id>
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from conftest import (
    COOKIES_PATH,
    beet_ls,
    cat_in_volume,
    ls_in_volume,
    put_bytes,
    run_fetch,
    run_scan,
    scan_binds_test,
)

pytestmark = pytest.mark.auth

_TEST_PLAYLIST_URL = os.environ.get("TEST_PLAYLIST_URL", "")
_TEST_PLAYLIST_NAME = "test-integration-small"

_MISSING_PREREQS = []
if not os.environ.get("SPOTIFY_CLIENT_ID"):
    _MISSING_PREREQS.append("SPOTIFY_CLIENT_ID not set")
if not COOKIES_PATH.exists():
    _MISSING_PREREQS.append(f"cookies.txt not found at {COOKIES_PATH}")
if not _TEST_PLAYLIST_URL:
    _MISSING_PREREQS.append("TEST_PLAYLIST_URL not set")

_SKIP_REASON = "; ".join(_MISSING_PREREQS) + " — run via `just test-auth`"


@pytest.mark.skipif(bool(_MISSING_PREREQS), reason=_SKIP_REASON)
def test_full_ingest_spotify(docker_client, volumes, beets_asis_config):
    """End-to-end: spotdl sync → beets import → source tag → .m3u generation."""
    # Write a minimal .spotdl state with no songs so spotdl actually downloads
    put_bytes(
        docker_client,
        volumes,
        f"/root/Music/inbox/spotdl/{_TEST_PLAYLIST_NAME}.spotdl",
        json.dumps({
            "type": "sync",
            "query": [_TEST_PLAYLIST_URL],
            "songs": [],
        }).encode(),
    )

    # Write a playlists.conf containing only our test playlist
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".conf", delete=False, prefix="test-playlists-"
    ) as f:
        f.write(f"{_TEST_PLAYLIST_NAME}  {_TEST_PLAYLIST_URL}\n")
        test_conf = f.name

    try:
        fetch_exit, fetch_logs = run_fetch(
            docker_client,
            volumes,
            env={
                "SPOTIFY_CLIENT_ID": os.environ["SPOTIFY_CLIENT_ID"],
                "SPOTIFY_CLIENT_SECRET": os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
            },
            playlists_conf=test_conf,
        )
    finally:
        Path(test_conf).unlink(missing_ok=True)

    assert fetch_exit == 0, f"music-ingest exited {fetch_exit}. Logs:\n{fetch_logs}"

    # Inbox should have downloaded files
    inbox_files = ls_in_volume(
        docker_client, volumes,
        f"/root/Music/inbox/spotdl/{_TEST_PLAYLIST_NAME}",
    )
    assert inbox_files, (
        "No files downloaded to inbox after music-ingest.\n"
        f"Fetch logs:\n{fetch_logs}"
    )

    # Run scan with asis config: this test validates auth and download, not MusicBrainz matching
    scan_exit, scan_logs = run_scan(docker_client, volumes, binds=scan_binds_test(volumes, beets_asis_config))
    assert scan_exit == 0, f"music-scan exited {scan_exit}. Logs:\n{scan_logs}"

    # All downloaded tracks should be tagged with the correct source
    tagged = beet_ls(docker_client, volumes, f"source:{_TEST_PLAYLIST_NAME}")
    assert tagged.strip(), (
        f"No tracks tagged source={_TEST_PLAYLIST_NAME} after scan.\n"
        f"Scan logs:\n{scan_logs}"
    )

    # .m3u must exist and be non-empty
    m3u = cat_in_volume(
        docker_client, volumes,
        f"/root/Music/playlists/{_TEST_PLAYLIST_NAME}.m3u",
    )
    assert m3u.strip(), (
        f"{_TEST_PLAYLIST_NAME}.m3u is empty after scan.\n"
        f"Scan logs:\n{scan_logs}"
    )

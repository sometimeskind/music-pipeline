"""Shared fixtures and helpers for container integration tests.

Tests orchestrate real Docker containers from the host. The image under test is
controlled by the SCAN_IMAGE / FETCH_IMAGE environment variables so CI can point
them at a freshly-built local image before the push to GHCR.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

import docker
import pytest
import yaml

# ---------------------------------------------------------------------------
# Image names — override via environment for CI
# ---------------------------------------------------------------------------

SCAN_IMAGE = os.environ.get(
    "SCAN_IMAGE", "ghcr.io/sometimeskind/music-pipeline-scan:latest"
)
FETCH_IMAGE = os.environ.get(
    "FETCH_IMAGE", "ghcr.io/sometimeskind/music-pipeline-fetch:latest"
)

REPO_ROOT = Path(__file__).parent.parent
BEETS_CONFIG = str(REPO_ROOT / "config" / "beets" / "config.yaml")
SPOTDL_CONFIG = str(REPO_ROOT / "config" / "spotdl" / "config.json")
PLAYLISTS_CONF = str(REPO_ROOT / "config" / "playlists.conf")
COOKIES_PATH = REPO_ROOT / "cookies.txt"

# ---------------------------------------------------------------------------
# Audio fixture — CC BY-NC-SA 3.0 track downloaded at test time, cached locally.
# "7 Ghosts I" by Nine Inch Nails, from Ghosts I–IV (2008).
# MusicBrainz Recording ID: 1d1bb32a-5bc6-4b6f-88cc-c043f6c52509
# Hosted on Internet Archive (long-term preservation guaranteed).
# ---------------------------------------------------------------------------

FIXTURE_AUDIO_URL = (
    "https://archive.org/download/nineinchnails_ghosts_I_IV/07_Ghosts_I.mp3"
)
FIXTURE_AUDIO_PATH = (
    Path(__file__).parent / "fixtures" / "audio" / "Nine Inch Nails - 7 Ghosts I.mp3"
)


# ---------------------------------------------------------------------------
# Session-scoped Docker client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


# ---------------------------------------------------------------------------
# Per-test isolated volumes
# ---------------------------------------------------------------------------


@pytest.fixture
def volumes(docker_client):
    """Fresh Docker volumes for each test, torn down afterwards."""
    music = docker_client.volumes.create()
    beets = docker_client.volumes.create()
    yield {"music": music.name, "beets": beets.name}
    docker_client.volumes.get(music.name).remove(force=True)
    docker_client.volumes.get(beets.name).remove(force=True)


# ---------------------------------------------------------------------------
# Audio fixture
# ---------------------------------------------------------------------------


def _download_fixture(url: str, dest: Path, retries: int = 4) -> None:
    """Download url to dest, retrying with exponential backoff on transient errors."""
    for attempt in range(retries):
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)  # 2, 4, 8, 16 s
            print(f"\nFixture download failed ({exc}), retrying in {wait}s…")
            time.sleep(wait)


@pytest.fixture(scope="session")
def fixture_audio():
    """Return path to the CC test track, downloading it if not already cached."""
    if not FIXTURE_AUDIO_PATH.exists():
        print(f"\nDownloading test fixture from {FIXTURE_AUDIO_URL} …")
        _download_fixture(FIXTURE_AUDIO_URL, FIXTURE_AUDIO_PATH)
    return FIXTURE_AUDIO_PATH


# ---------------------------------------------------------------------------
# Beets test config — derived from the production config at runtime.
#
# Only two values are overridden so import tests are deterministic in CI:
#   strong_rec_thresh: 0.30   — accepts a good text match; avoids relying on
#                               AcoustID fingerprint lookup in the database
#   chroma plugin removed     — no AcoustID network calls (CI stability)
#
# Everything else (paths, plugins, fetchart, etc.) comes from the production
# config, so this fixture tracks production automatically — no config drift.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def beets_test_config(tmp_path_factory):
    """Production beets config + test overrides, written to a session-scoped tmp file."""
    with open(BEETS_CONFIG) as f:
        config = yaml.safe_load(f)

    config.setdefault("match", {})["strong_rec_thresh"] = 0.30

    plugins = config.get("plugins", [])
    if isinstance(plugins, str):
        plugins = plugins.split()
    config["plugins"] = [p for p in plugins if p != "chroma"]

    tmp = tmp_path_factory.mktemp("beets-cfg") / "config.yaml"
    tmp.write_text(yaml.dump(config))
    return str(tmp)


# ---------------------------------------------------------------------------
# Volume bind helpers
# ---------------------------------------------------------------------------


def scan_binds(vol_names: dict) -> dict:
    """Standard volume/mount mapping for the scan container (production config)."""
    return {
        vol_names["music"]: {"bind": "/root/Music", "mode": "rw"},
        vol_names["beets"]: {"bind": "/root/.config/beets", "mode": "rw"},
        BEETS_CONFIG: {"bind": "/root/.config/beets/config.yaml", "mode": "ro"},
    }


def scan_binds_test(vol_names: dict, config_path: str) -> dict:
    """scan_binds variant that mounts a generated test beets config."""
    return {
        vol_names["music"]: {"bind": "/root/Music", "mode": "rw"},
        vol_names["beets"]: {"bind": "/root/.config/beets", "mode": "rw"},
        config_path: {"bind": "/root/.config/beets/config.yaml", "mode": "ro"},
    }


def fetch_binds(vol_names: dict, playlists_conf: str | None = None) -> dict:
    """Standard volume/mount mapping for the fetch container."""
    return {
        vol_names["music"]: {"bind": "/root/Music", "mode": "rw"},
        SPOTDL_CONFIG: {"bind": "/root/.config/spotdl/config.json", "mode": "ro"},
        (playlists_conf or PLAYLISTS_CONF): {
            "bind": "/root/.config/music-pipeline/playlists.conf",
            "mode": "ro",
        },
        str(COOKIES_PATH): {"bind": "/root/.config/spotdl/cookies.txt", "mode": "ro"},
    }


# ---------------------------------------------------------------------------
# File injection helpers
# ---------------------------------------------------------------------------


def put_file(client, vol_names: dict, container_dir: str, local_path: Path) -> None:
    """Copy a local file into container_dir inside the music volume."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(local_path), arcname=local_path.name)
    buf.seek(0)

    helper = client.containers.create(
        SCAN_IMAGE,
        command=["sleep", "30"],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "rw"}},
    )
    helper.start()
    try:
        helper.put_archive(container_dir, buf)
    finally:
        helper.kill()
        helper.remove(force=True)


def put_bytes(
    client, vol_names: dict, container_path: str, data: bytes
) -> None:
    """Write raw bytes to container_path inside the music volume."""
    p = PurePosixPath(container_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=p.name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)

    helper = client.containers.create(
        SCAN_IMAGE,
        command=["sleep", "30"],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "rw"}},
    )
    helper.start()
    try:
        helper.put_archive(str(p.parent), buf)
    finally:
        helper.kill()
        helper.remove(force=True)


def mkdir_in_volume(client, vol_names: dict, container_path: str) -> None:
    """Create a directory (and parents) inside the music volume."""
    client.containers.run(
        SCAN_IMAGE,
        command=["mkdir", "-p", container_path],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "rw"}},
        remove=True,
    )


# ---------------------------------------------------------------------------
# Container run helpers
# ---------------------------------------------------------------------------


def run_scan(
    client, vol_names: dict, env: dict | None = None, binds: dict | None = None
) -> tuple[int, str]:
    """Run music-scan to completion. Returns (exit_code, combined_logs).

    Pass `binds=scan_binds_test(vol_names, beets_test_config)` to use the
    test beets config instead of the production one.
    """
    c = client.containers.create(
        SCAN_IMAGE,
        environment={"PUSHGATEWAY_URL": "", **(env or {})},
        volumes=binds if binds is not None else scan_binds(vol_names),
    )
    c.start()
    exit_code = c.wait()["StatusCode"]
    logs = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    return exit_code, logs


def run_fetch(
    client,
    vol_names: dict,
    env: dict,
    playlists_conf: str | None = None,
) -> tuple[int, str]:
    """Run music-ingest to completion. Returns (exit_code, combined_logs)."""
    c = client.containers.create(
        FETCH_IMAGE,
        environment={"PUSHGATEWAY_URL": "", **env},
        volumes=fetch_binds(vol_names, playlists_conf=playlists_conf),
    )
    c.start()
    exit_code = c.wait()["StatusCode"]
    logs = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    return exit_code, logs


def ls_in_volume(client, vol_names: dict, container_path: str) -> list[str]:
    """Return file paths found under container_path in the music volume."""
    result = client.containers.run(
        SCAN_IMAGE,
        command=["find", container_path, "-type", "f"],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "ro"}},
        remove=True,
    )
    lines = result.decode().strip().splitlines()
    return [l for l in lines if l]


def beet_ls(client, vol_names: dict, query: str) -> str:
    """Run `beet ls -a <query>` inside the scan container and return stdout."""
    c = client.containers.create(
        SCAN_IMAGE,
        entrypoint=["beet"],
        command=["ls", "-a", query],
        volumes=scan_binds(vol_names),
    )
    c.start()
    c.wait()
    output = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    return output


def cat_in_volume(client, vol_names: dict, container_path: str) -> str:
    """Return the text content of a file in the music volume."""
    result = client.containers.run(
        SCAN_IMAGE,
        command=["cat", container_path],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "ro"}},
        remove=True,
    )
    return result.decode()

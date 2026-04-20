"""Shared fixtures and helpers for container integration tests.

Tests orchestrate real Docker containers from the host. The image under test is
controlled by the SCAN_IMAGE / FETCH_IMAGE environment variables so CI can point
them at a freshly-built local image before the push to GHCR.
"""

from __future__ import annotations

import io
import os
import tarfile
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
SERVICE_IMAGE = os.environ.get(
    "SERVICE_IMAGE", "ghcr.io/sometimeskind/music-pipeline:latest"
)

REPO_ROOT = Path(__file__).parent.parent
BEETS_CONFIG = str(REPO_ROOT / "config" / "beets" / "config.yaml")
SPOTDL_CONFIG = str(REPO_ROOT / "config" / "spotdl" / "config.json")
PLAYLISTS_CONF = str(REPO_ROOT / "config" / "playlists.conf")
COOKIES_PATH = REPO_ROOT / "cookies.txt"

# ---------------------------------------------------------------------------
# Audio fixture — synthetic silent MP3 generated at test time by ffmpeg.
#
# Named after "7 Ghosts I" by Nine Inch Nails (CC BY-NC-SA 3.0).
# MusicBrainz Recording ID: 1d1bb32a-5bc6-4b6f-88cc-c043f6c52509
#
# beets matches on embedded artist/title/album tags + duration (no AcoustID).
# Duration is set to 121 s (the real track length) to keep distance low.
# No external downloads required — ffmpeg is already in the scan image.
# ---------------------------------------------------------------------------

_FIXTURE_FILENAME = "Nine Inch Nails - 7 Ghosts I.mp3"
_FIXTURE_DURATION_S = 121  # matches MusicBrainz track length (2:01)


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


@pytest.fixture(scope="session")
def fixture_audio(docker_client, tmp_path_factory):
    """Generate a silent MP3 named after a MusicBrainz-indexed track.

    Uses ffmpeg inside the scan container — no external downloads.
    Embedded tags (artist, title, album) + matching duration give beets
    enough signal for a confident text-only match against MusicBrainz.
    """
    dest_dir = tmp_path_factory.mktemp("audio")
    dest = dest_dir / _FIXTURE_FILENAME
    docker_client.containers.run(
        SCAN_IMAGE,
        command=[
            "ffmpeg",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(_FIXTURE_DURATION_S),
            "-metadata", "artist=Nine Inch Nails",
            "-metadata", "title=7 Ghosts I",
            "-metadata", "album=Ghosts I-IV",
            "-q:a", "9", "-y",
            f"/out/{_FIXTURE_FILENAME}",
        ],
        volumes={str(dest_dir): {"bind": "/out", "mode": "rw"}},
        remove=True,
    )
    assert dest.exists(), f"ffmpeg failed to produce {dest}"
    return dest


# ---------------------------------------------------------------------------
# Beets test configs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def beets_asis_config(tmp_path_factory):
    """Production config stripped down for network-free asis testing.

    Changes from the production config:
    - autotag=False: import uses embedded tags, no MusicBrainz lookups
    - plugins reduced to [music_pipeline]: fetchart, chroma, embedart, scrub,
      etc. are removed to avoid any network calls (fetchart auto-fetches art
      from musicbrainz.org / fanart.tv even with autotag=False)

    import_task_created still fires (it fires in all modes), so the
    music_pipeline plugin writes source= and via= to the library.
    Duplicate handling uses duplicate_action from config (beets built-in).
    """
    with open(BEETS_CONFIG) as f:
        config = yaml.safe_load(f)

    config.setdefault("import", {})["autotag"] = False
    config["plugins"] = ["music_pipeline"]

    tmp = tmp_path_factory.mktemp("beets-cfg-asis") / "config.yaml"
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
        command=["sleep", "infinity"],
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
        command=["sleep", "infinity"],
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
    client,
    vol_names: dict,
    env: dict | None = None,
    binds: dict | None = None,
    image: str | None = None,
) -> tuple[int, str]:
    """Run music-scan to completion. Returns (exit_code, combined_logs).

    Pass `binds=scan_binds_test(vol_names, beets_test_config)` to use the
    test beets config instead of the production one.
    Pass `image=SERVICE_IMAGE` to run against the unified service image instead
    of the standalone scan image.
    """
    c = client.containers.create(
        image or SCAN_IMAGE,
        command=["music-scan"],
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


def ls_in_volume(
    client, vol_names: dict, container_path: str, image: str | None = None
) -> list[str]:
    """Return file paths found under container_path in the music volume."""
    result = client.containers.run(
        image or SCAN_IMAGE,
        command=["find", container_path, "-type", "f"],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "ro"}},
        remove=True,
    )
    lines = result.decode().strip().splitlines()
    return [line for line in lines if line]


def beet_ls(client, vol_names: dict, query: str, image: str | None = None) -> str:
    """Run `beet ls <query>` inside the scan container and return stdout.

    Queries items (not albums) so that item-level flexible attributes such as
    source= are matched correctly.
    """
    c = client.containers.create(
        image or SCAN_IMAGE,
        entrypoint=["beet"],
        command=["ls", query],
        volumes=scan_binds(vol_names),
    )
    c.start()
    c.wait()
    output = c.logs(stdout=True, stderr=True).decode()
    c.remove(force=True)
    return output


def cat_in_volume(
    client, vol_names: dict, container_path: str, image: str | None = None
) -> str:
    """Return the text content of a file in the music volume."""
    result = client.containers.run(
        image or SCAN_IMAGE,
        command=["cat", container_path],
        volumes={vol_names["music"]: {"bind": "/root/Music", "mode": "ro"}},
        remove=True,
    )
    return result.decode()


# ---------------------------------------------------------------------------
# Service container fixtures and helpers
# ---------------------------------------------------------------------------


_SERVICE_ENV_BASE = {
    "API_BEARER_TOKEN": "test-token",
    "SPOTIFY_CLIENT_ID": "fake",
    "SPOTIFY_CLIENT_SECRET": "fake",
}


def _start_service(docker_client, volumes, beets_config_path):
    """Context manager: start service container, wait for health, yield svc dict.

    Yields a dict with:
      - base_url: http://localhost:8080
      - headers:  Authorization bearer headers for authenticated requests
      - container: the running Container object (for logs / exec_run)
      - volumes:  the vol_names dict
    """
    import time
    import requests as _requests
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        container = docker_client.containers.run(
            SERVICE_IMAGE,
            command=["music-pipeline"],
            detach=True,
            network_mode="host",
            environment=_SERVICE_ENV_BASE,
            volumes={
                volumes["music"]: {"bind": "/root/Music", "mode": "rw"},
                volumes["beets"]: {"bind": "/root/.config/beets", "mode": "rw"},
                beets_config_path: {"bind": "/root/.config/beets/config.yaml", "mode": "ro"},
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
            }
        finally:
            container.stop(timeout=5)
            container.remove(force=True)

    return _ctx()


@pytest.fixture
def running_service(docker_client, volumes):
    """Start the service container with the production beets config."""
    with _start_service(docker_client, volumes, BEETS_CONFIG) as svc:
        yield svc


@pytest.fixture
def running_service_asis(docker_client, volumes, beets_asis_config):
    """Start the service container with asis beets config (no MusicBrainz calls)."""
    with _start_service(docker_client, volumes, beets_asis_config) as svc:
        yield svc


def service_in_container(container, command: list[str]) -> tuple[int, str]:
    """Run a command inside a running service container via the Docker SDK.

    Returns (exit_code, output).
    """
    result = container.exec_run(command, stdout=True, stderr=True)
    return result.exit_code, result.output.decode()


def wait_for_log(container, sentinel: str, timeout: float = 60) -> bool:
    """Stream container logs until *sentinel* appears or *timeout* seconds elapse.

    Returns True if the sentinel was found, False on timeout.

    Uses a daemon thread so the timeout is enforced even when the container is
    idle (no log chunks arriving). Without a thread the timeout check only fires
    after each chunk, which can hang indefinitely if the container goes quiet.
    """
    import threading

    found = threading.Event()

    def _stream():
        try:
            for chunk in container.logs(stream=True, follow=True):
                if sentinel.encode() in chunk:
                    found.set()
                    return
                if found.is_set():
                    return
        except Exception:
            pass

    threading.Thread(target=_stream, daemon=True).start()
    return found.wait(timeout=timeout)

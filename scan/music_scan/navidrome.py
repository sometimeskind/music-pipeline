"""Navidrome: trigger a library rescan (Subsonic API) and sweep the playlists it
imported from ``.m3u`` files the pipeline no longer writes (native API)."""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath

import requests

logger = logging.getLogger(__name__)

# Where Navidrome sees the pipeline's playlists/ (homelab mounts the music
# volume's playlists subPath at /music/playlists in the navidrome pod).
DEFAULT_PLAYLISTS_DIR = "/music/playlists"


def _config() -> tuple[str, str, str] | None:
    """``(url, user, password)`` from the environment; None when NAVIDROME_URL is unset."""
    url = os.environ.get("NAVIDROME_URL", "")
    user = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")

    if not url:
        return None

    if not user or not password:
        raise RuntimeError(
            "NAVIDROME_URL is set but NAVIDROME_USER or NAVIDROME_PASSWORD is missing"
        )
    return url.rstrip("/"), user, password


def trigger_scan() -> None:
    """POST a startScan request to Navidrome if NAVIDROME_URL is configured.

    Reads NAVIDROME_URL, NAVIDROME_USER, and NAVIDROME_PASSWORD from the
    environment. If any are absent, logs a debug message and returns silently —
    the caller does not need to guard against a missing configuration.
    """
    config = _config()
    if config is None:
        return
    url, user, password = config

    endpoint = f"{url}/rest/startScan.view"
    params = {
        "u": user,
        "p": password,
        "v": "1.8.0",
        "c": "music-pipeline",
        "f": "json",
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to trigger Navidrome rescan: {exc}") from exc
    data = resp.json()
    status = data.get("subsonic-response", {}).get("status")
    if status != "ok":
        raise RuntimeError(f"Navidrome rescan returned non-ok status: {data}")
    logger.info("==> Navidrome library rescan triggered")


def _login(url: str, user: str, password: str) -> dict[str, str]:
    """Log in to Navidrome's native API; return the request headers carrying its JWT.

    The Subsonic API's playlists carry no file path, so the sweep uses the
    native API (``/api/playlist``), which takes a token from ``/auth/login``.
    """
    try:
        resp = requests.post(f"{url}/auth/login", json={"username": user, "password": password}, timeout=10)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Navidrome login failed: {exc}") from exc
    if not body.get("token"):
        raise RuntimeError("Navidrome login returned no token")
    if not body.get("isAdmin"):
        # The scanner imports playlists as Navidrome's first admin, private by
        # default: a non-admin user neither sees nor may delete them.
        logger.warning("[NAVIDROME] %s is not a Navidrome admin: the playlists Navidrome imported "
                       "are hidden from it, so the sweep can't remove them", user)
    return {"X-ND-Authorization": f"Bearer {body['token']}"}


def sweep_playlists(playlists_dir: Path, keep: set[str]) -> list[str]:
    """Delete the Navidrome playlists imported from a file under ``playlists/`` that is gone.

    Navidrome imports each ``.m3u`` under *playlists_dir* as a synced playlist,
    and keeps it when the file disappears (homelab#2541).  A playlist is deleted
    when Navidrome's record of it has a path in its view of ``playlists/``
    (``NAVIDROME_PLAYLISTS_DIR``), sync on, and no file of that name in
    *playlists_dir*.  It is matched by that path, never by name: a playlist made
    in Navidrome has no path and is never touched.  Nor is one whose name is in
    *keep* (``playlists.conf``): its file is due back.  A playlist the operator
    turned sync off on is theirs and stays.

    Every nightly sweeps, so a run that couldn't reach Navidrome is retried by
    the next.  Returns the names deleted.  Raises RuntimeError when Navidrome
    can't be logged in to or listed; a failed delete is logged and skipped.
    """
    config = _config()
    if config is None:
        return []
    if not playlists_dir.is_dir():
        logger.warning("[NAVIDROME] %s is not a directory: playlist sweep skipped", playlists_dir)
        return []
    url, user, password = config
    nd_dir = PurePosixPath(os.environ.get("NAVIDROME_PLAYLISTS_DIR") or DEFAULT_PLAYLISTS_DIR)

    headers = _login(url, user, password)
    try:
        resp = requests.get(f"{url}/api/playlist", headers=headers, timeout=30)
        resp.raise_for_status()
        playlists = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Could not list Navidrome playlists: {exc}") from exc

    deleted = []
    for pls in playlists:
        if not pls.get("path"):
            continue  # made in Navidrome, not imported from a file
        path = PurePosixPath(pls["path"])
        if path.parent != nd_dir or (playlists_dir / path.name).exists():
            continue
        name = pls.get("name", "")
        if path.stem in keep:
            logger.warning("[NAVIDROME] %s: %s is gone but %s is in playlists.conf; left for its next .m3u",
                           name, path, path.stem)
            continue
        if not pls.get("sync"):
            logger.info("[NAVIDROME] %s: %s is gone, but sync is off in Navidrome; left", name, path)
            continue
        try:
            requests.delete(f"{url}/api/playlist/{pls['id']}", headers=headers, timeout=10).raise_for_status()
        except requests.RequestException as exc:
            logger.error("[NAVIDROME] could not delete playlist %s (imported from %s): %s; the next nightly retries",
                         name, path, exc)
            continue
        logger.info("[NAVIDROME] deleted playlist %s: imported from %s, which is gone", name, path)
        deleted.append(name)
    return deleted

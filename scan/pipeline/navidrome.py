"""Notify Navidrome to trigger a library rescan via the Subsonic API."""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


def trigger_scan() -> None:
    """POST a startScan request to Navidrome if NAVIDROME_URL is configured.

    Reads NAVIDROME_URL, NAVIDROME_USER, and NAVIDROME_PASSWORD from the
    environment. If any are absent, logs a debug message and returns silently —
    the caller does not need to guard against a missing configuration.
    """
    url = os.environ.get("NAVIDROME_URL", "")
    user = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")

    if not url:
        return

    if not user or not password:
        logger.warning("NAVIDROME_URL is set but NAVIDROME_USER or NAVIDROME_PASSWORD is missing — skipping rescan")
        return

    endpoint = f"{url.rstrip('/')}/rest/startScan.view"
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
        data = resp.json()
        status = data.get("subsonic-response", {}).get("status")
        if status != "ok":
            logger.warning("Navidrome rescan returned non-ok status: %s", data)
        else:
            logger.info("==> Navidrome library rescan triggered")
    except requests.RequestException as exc:
        logger.warning("Failed to trigger Navidrome rescan: %s", exc)

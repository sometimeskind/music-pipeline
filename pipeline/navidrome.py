"""Navidrome Subsonic API client — trigger library rescans."""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


def trigger_rescan() -> bool:
    """Trigger a Navidrome library rescan via the Subsonic API.

    Returns True on success, False on failure (non-fatal).
    Reads NAVIDROME_URL and NAVIDROME_API_KEY (``user:password``) from env.
    No-ops if either variable is unset.
    """
    url = os.environ.get("NAVIDROME_URL", "")
    api_key = os.environ.get("NAVIDROME_API_KEY", "")
    if not url or not api_key:
        return True

    user, _, password = api_key.partition(":")
    endpoint = (
        f"{url.rstrip('/')}/rest/startScan"
        f"?u={user}&p={password}&v=1.16.1&c=music-pipeline&f=json"
    )

    logger.info("Triggering Navidrome library rescan...")
    try:
        response = requests.get(endpoint, timeout=15)
        response.raise_for_status()
        data = response.json()
        status = data.get("subsonic-response", {}).get("status", "unknown")
        if status == "ok":
            logger.info("Navidrome rescan triggered successfully.")
            return True
        logger.warning("Navidrome rescan returned unexpected status: %s — %s", status, data)
        return False
    except requests.RequestException as exc:
        logger.warning("Navidrome rescan request failed: %s", exc)
        return False

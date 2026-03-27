"""Prometheus text-format metric builders and Pushgateway push."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


def _gauge(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    label_str = ""
    if labels:
        pairs = ",".join(f'{k}="{v}"' for k, v in labels.items())
        label_str = f"{{{pairs}}}"
    return f"# TYPE {name} gauge\n{name}{label_str} {value}"


def _push(body: str, job: str) -> None:
    url = os.environ.get("PUSHGATEWAY_URL", "")
    if not url:
        return
    endpoint = f"{url.rstrip('/')}/metrics/job/{job}"
    try:
        resp = requests.post(endpoint, data=body.encode(), timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to push metrics to %s: %s", endpoint, exc)


@dataclass
class IngestMetrics:
    success: bool = True
    duration_seconds: int = 0
    playlists_total: int = 0
    playlists_skipped: int = 0
    playlists_deferred: int = 0
    tracks_downloaded: int = 0
    cookies_expired: bool = False
    failure_reason: str = ""

    def push(self) -> None:
        lines = [
            _gauge("music_ingest_last_run_success", int(self.success)),
            _gauge("music_ingest_duration_seconds", self.duration_seconds),
            _gauge("music_ingest_playlists_total", self.playlists_total),
            _gauge("music_ingest_playlists_skipped_total", self.playlists_skipped),
            _gauge("music_ingest_playlists_deferred_total", self.playlists_deferred),
            _gauge("music_ingest_tracks_downloaded_total", self.tracks_downloaded),
            _gauge("music_ingest_cookies_expired", int(self.cookies_expired)),
        ]
        if not self.success and self.failure_reason:
            lines.append(
                _gauge("music_ingest_last_failure_reason", 1, {"reason": self.failure_reason})
            )
        _push("\n".join(lines), "music_ingest")

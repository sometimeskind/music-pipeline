"""Prometheus text-format metric builders and Pushgateway push."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

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
        resp = requests.put(endpoint, data=(body + "\n").encode(), timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to push metrics to %s: %s", endpoint, exc)


@dataclass
class ScanMetrics:
    success: bool = True
    duration_seconds: int = 0
    quarantined_tracks: int = 0
    tracks_imported: int = 0
    tracks_removed: int = 0
    failure_reason: str = ""
    lossless_items: int | None = None
    # Downloads the length guard rejected, per reason (#165).
    rejected: dict[str, int] = field(default_factory=lambda: {"duration": 0, "silence": 0})
    # Playlist entries with no library item, per playlist (#228).
    slots_empty: dict[str, int] = field(default_factory=dict)

    def push(self) -> None:
        lines = [
            _gauge("music_scan_last_run_success", int(self.success)),
            _gauge("music_scan_duration_seconds", self.duration_seconds),
            _gauge("music_scan_quarantined_tracks_total", self.quarantined_tracks),
            _gauge("music_scan_tracks_imported_total", self.tracks_imported),
            _gauge("music_scan_tracks_removed_total", self.tracks_removed),
        ]
        lines.append("# TYPE music_scan_rejected_tracks_total gauge")
        lines += [f'music_scan_rejected_tracks_total{{reason="{r}"}} {n}' for r, n in sorted(self.rejected.items())]
        if self.lossless_items is not None:
            lines.append(_gauge("music_library_lossless_items", self.lossless_items))
        if self.slots_empty:
            lines.append("# TYPE music_playlist_slots_empty gauge")
            lines += [f'music_playlist_slots_empty{{playlist="{p}"}} {n}' for p, n in sorted(self.slots_empty.items())]
        if not self.success and self.failure_reason:
            lines.append(
                _gauge("music_scan_last_failure_reason", 1, {"reason": self.failure_reason})
            )
        _push("\n".join(lines), "music_scan")

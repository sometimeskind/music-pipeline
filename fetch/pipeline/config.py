"""Playlist configuration: parsing playlists.conf and the PlaylistConfig dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONF = Path("/root/.config/music-pipeline/playlists.conf")


@dataclass
class PlaylistConfig:
    name: str
    url: str
    nosync: bool = False


def load_playlists(path: Path = DEFAULT_CONF) -> list[PlaylistConfig]:
    """Parse playlists.conf and return a list of PlaylistConfig entries.

    Format: one entry per line — ``name  spotify-url  [nosync]``
    Lines starting with # and blank lines are ignored.
    """
    playlists: list[PlaylistConfig] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            # malformed — caller can warn if desired
            continue
        playlists.append(
            PlaylistConfig(
                name=parts[0],
                url=parts[1],
                nosync=len(parts) >= 3 and parts[2] == "nosync",
            )
        )
    return playlists

"""Playlist configuration: parsing playlists.conf and the PlaylistConfig dataclass."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

from music_fetch import ids

DEFAULT_CONF = Path("/root/.config/music-pipeline/playlists.conf")


@dataclass
class PlaylistConfig:
    name: str
    url: str
    nosync: bool = False
    album: bool = False


def load_playlists(path: Path = DEFAULT_CONF) -> list[PlaylistConfig]:
    """Parse playlists.conf and return a list of PlaylistConfig entries.

    Format: one entry per line — ``name  spotify-id  [nosync|album]``
    Lines starting with # and blank lines are ignored.  The playlist is a
    Spotify ID or an ``open.spotify.com`` playlist or album URL
    (homelab#2411), held as its canonical URL; anything else raises
    ``ValueError``.
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
                url=_spotify_url(parts[1]),
                nosync="nosync" in parts[2:],
                album="album" in parts[2:],
            )
        )
    names = [pl.name for pl in playlists]
    for a, b in itertools.combinations(names, 2):
        if a in b or b in a:
            raise ValueError(
                f"Playlist name clash: '{a}' is a substring of '{b}' — "
                "beets substring queries would produce false positives"
            )
    return playlists


def _spotify_url(ref: str) -> str:
    """The canonical URL of a playlists.conf entry's Spotify ID or URL (a bare
    ID is a playlist's)."""
    kind, id_ = ids.spotify_ref(ref, ("playlist", "album"))
    return ids.spotify_url(kind or "playlist", id_)

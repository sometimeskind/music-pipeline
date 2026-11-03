"""Playlist configuration: parsing playlists.conf and the PlaylistConfig dataclass."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

from music_fetch import ids

DEFAULT_CONF = Path("/root/.config/music-pipeline/playlists.conf")


# Flags that can't be combined with twoway: a playlist the sync may write to
# Spotify must be a live, track-level one that is allowed to go two-way.
_NOT_TWOWAY = ("oneway", "nosync", "album")


@dataclass
class PlaylistConfig:
    name: str
    url: str
    nosync: bool = False
    album: bool = False
    # homelab#2361: twoway opts a playlist into the two-way Navidrome/Spotify sync
    # (no playlist is two-way without it); oneway locks it to one-way for good.
    # Neither changes anything until the sync ships.
    twoway: bool = False
    oneway: bool = False


def load_playlists(path: Path = DEFAULT_CONF) -> list[PlaylistConfig]:
    """Parse playlists.conf and return a list of PlaylistConfig entries.

    Format: one entry per line — ``name  spotify-id  [flag ...]``, the flags
    ``nosync``, ``album``, ``twoway`` and ``oneway`` (others are ignored).
    Lines starting with # and blank lines are ignored.  The playlist is a
    Spotify ID or an ``open.spotify.com`` playlist or album URL
    (homelab#2411), held as its canonical URL; anything else raises
    ``ValueError``, and so does ``twoway`` with ``oneway``, ``nosync`` or
    ``album`` (homelab#2361): a config error stops the load rather than
    guess which flag was meant.
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
        flags = set(parts[2:])
        if "twoway" in flags and (clash := [f for f in _NOT_TWOWAY if f in flags]):
            raise ValueError(f"Playlist '{parts[0]}': twoway can't be combined with {', '.join(clash)}")
        playlists.append(
            PlaylistConfig(
                name=parts[0],
                url=_spotify_url(parts[1]),
                nosync="nosync" in flags,
                album="album" in flags,
                twoway="twoway" in flags,
                oneway="oneway" in flags,
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

"""Downloads the nightly must not fetch again (homelab#1914).

A spotdl download carries its Spotify URL in the ``----:spotdl:WOAS`` tag.
One sitting in quarantine was rejected (the length guard, a weak
MusicBrainz match) and is never retried: spotdl would likely pick the same
wrong video and loop (#165).  One still in a playlist's inbox waits for an
import (a MusicBrainz outage, #264).  Either way no library item holds it
yet, so the playlist store wants it: these URLs are held back.
"""

from __future__ import annotations

from pathlib import Path

QUARANTINE_DIR = Path("/root/Music/quarantine")
SPOTDL_DIR = Path("/root/Music/inbox/spotdl")


def _read_spotify_url(path: Path) -> str | None:
    """Return the Spotify URL embedded in an M4A file by spotdl, or None."""
    try:
        from mutagen.mp4 import MP4  # noqa: PLC0415 — beets dep, always available

        audio = MP4(str(path))
        raw = (audio.tags or {}).get("----:spotdl:WOAS")
        if raw:
            return raw[0].decode("utf-8")
    except Exception:
        pass
    return None


def spotify_urls(directory: Path) -> frozenset[str]:
    """The Spotify URLs of the spotdl downloads under *directory*."""
    urls: set[str] = set()
    for m4a in directory.rglob("*.m4a"):
        url = _read_spotify_url(m4a)
        if url:
            urls.add(url)
    return frozenset(urls)


def held_urls(quarantine_dir: Path = QUARANTINE_DIR, inbox_dir: Path = SPOTDL_DIR) -> frozenset[str]:
    """The URLs downloaded into quarantine or still waiting in the inbox."""
    return spotify_urls(quarantine_dir) | spotify_urls(inbox_dir)

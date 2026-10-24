"""Downloads the nightly must not fetch again (homelab#1914).

A spotdl download carries its Spotify URL in the ``----:spotdl:WOAS`` tag.
One sitting in quarantine was rejected (the length guard, a weak
MusicBrainz match) and is never retried: spotdl would likely pick the same
wrong video and loop (#165).  One still in a playlist's inbox waits for an
import (a MusicBrainz outage, #264).  Either way no library item holds it
yet, so the playlist store wants it: these URLs are held back.

A track of an album album mode is about to grab for an ``albums-first``
playlist (#324) is held too, while the grab or its import is pending: the
nightly's budget goes to the other tracks.
"""

from __future__ import annotations

from pathlib import Path

QUARANTINE_DIR = Path("/root/Music/quarantine")
SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
SPOTIFY_TRACK_URL = "https://open.spotify.com/track/"


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


def album_held(state_file: Path | None = None) -> frozenset[str]:
    """The URLs of the tracks album mode holds for an albums-first playlist
    (:func:`music_fetch.albums.held_tracks`), only while ``ALBUM_MODE`` is
    ``on``: in ``off`` or ``dry-run`` nothing grabs them.  An unreadable
    state holds nothing."""
    from music_fetch import albums  # noqa: PLC0415

    if albums.Settings.from_env().mode != "on":
        return frozenset()
    try:
        state = albums.State.load(state_file or albums.STATE_FILE)
    except (OSError, ValueError):
        return frozenset()
    return frozenset(SPOTIFY_TRACK_URL + track_id for track_id in albums.held_tracks(state))

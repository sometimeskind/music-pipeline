"""Canonical YouTube and Spotify IDs (homelab#2411).

Wherever an operator, a config or an MCP endpoint names an external item, the
pipeline takes an ID, or a URL it reduces to one, and builds the URL it uses
itself.  Nothing else reaches spotdl or yt-dlp: a URL from attacker-influenced
text (a release or file name) can't make yt-dlp fetch from another site.

- YouTube: an 11-character video ID, or a ``youtube.com``, ``www.youtube.com``,
  ``music.youtube.com`` or ``youtu.be`` URL naming one (YouTube Music shares
  video IDs).  Playlists and albums (``list=`` alone, ``OLAK5uy_…``,
  ``browse/MPREb_…``) and other sites are rejected; query parameters other than
  ``v`` (``t=``, ``list=``, ``si=``) are dropped.
- Spotify: a 22-character base62 ID, or an ``open.spotify.com/<kind>/<id>`` URL
  (with or without ``intl-xx/``, any query string dropped).

Each parser raises :class:`ValueError` on anything else.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com"}
# Paths whose second segment is the video ID: youtube.com/shorts/<id> and the like.
_YOUTUBE_ID_PATHS = {"shorts", "embed", "live"}
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v="

_SPOTIFY_ID = re.compile(r"[0-9A-Za-z]{22}")
_SPOTIFY_HOST = "open.spotify.com"
_SPOTIFY_INTL = re.compile(r"intl-[a-z]{2}(-[a-z]{2})?")
SPOTIFY_URL = "https://open.spotify.com/"


def youtube_id(ref: str) -> str:
    """The video ID in *ref*: an ID, or a YouTube URL naming one video."""
    ref = ref.strip()
    if _YOUTUBE_ID.fullmatch(ref):
        return ref
    parsed = urlsplit(ref)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"not a YouTube video ID or URL: {ref!r}")
    segments = [s for s in parsed.path.split("/") if s]
    video = None
    if host == "youtu.be" and len(segments) == 1:
        video = segments[0]
    elif host in _YOUTUBE_HOSTS and segments == ["watch"]:
        video = (parse_qs(parsed.query).get("v") or [None])[0]
    elif host in _YOUTUBE_HOSTS and len(segments) == 2 and segments[0] in _YOUTUBE_ID_PATHS:
        video = segments[1]
    if video is None or not _YOUTUBE_ID.fullmatch(video):
        raise ValueError(f"not a YouTube video ID or URL: {ref!r}")
    return video


def youtube_url(ref: str) -> str:
    """The canonical watch URL for *ref* (see :func:`youtube_id`): all yt-dlp is given."""
    return YOUTUBE_WATCH_URL + youtube_id(ref)


def spotify_ref(ref: str, kinds: tuple[str, ...]) -> tuple[str | None, str]:
    """``(kind, id)`` for *ref*: a bare ID (kind ``None``) or an
    ``open.spotify.com`` URL of one of *kinds*."""
    ref = ref.strip()
    if _SPOTIFY_ID.fullmatch(ref):
        return None, ref
    parsed = urlsplit(ref)
    segments = [s for s in parsed.path.split("/") if s]
    if segments and _SPOTIFY_INTL.fullmatch(segments[0]):
        segments = segments[1:]
    if (parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() == _SPOTIFY_HOST
            and len(segments) == 2 and segments[0] in kinds and _SPOTIFY_ID.fullmatch(segments[1])):
        return segments[0], segments[1]
    raise ValueError(f"not a Spotify {' or '.join(kinds)} ID or URL: {ref!r}")


def spotify_id(ref: str, kind: str) -> str:
    """The ID in *ref*: a bare ID, or an ``open.spotify.com/<kind>/`` URL."""
    return spotify_ref(ref, (kind,))[1]


def spotify_key(ref: str, kind: str) -> str:
    """*ref* reduced to its ID when it is a URL (which must then be an
    ``open.spotify.com/<kind>/`` one); any other text, an ID, an album key or
    search words, as it is."""
    return spotify_id(ref, kind) if "://" in ref else ref


def spotify_url(kind: str, id_: str) -> str:
    """The canonical ``open.spotify.com`` URL of a Spotify *kind* ID."""
    return f"{SPOTIFY_URL}{kind}/{id_}"

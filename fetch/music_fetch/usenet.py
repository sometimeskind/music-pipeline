"""Prowlarr and SABnzbd clients, and release ranking for album mode (#168).

Both services run in-cluster behind a SOCKS egress, so the pipeline only ever
talks to them, never to the indexer or the news server.  The one subtle step is
the grab: Prowlarr refuses Redirect off for Usenet indexers, so its download
link answers with a redirect to the indexer.  We read that Location without
following it and hand it to SABnzbd, which fetches it through its own proxy.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

AUDIO_CATEGORY = 3000
LOSSLESS_CATEGORY = 3040
SAB_CATEGORY = "album"

# Size sanity per album track: below this a release is a single or a sampler;
# above it, a discography or a video.
MIN_BYTES_PER_TRACK = 1_000_000
MAX_BYTES_PER_TRACK = 150_000_000

# Tiers, best first.  The library is AAC 256k: lossless transcodes cleanly,
# MP3 320 is kept as-is by beets (never_convert_lossy_files).
TIER_LOSSLESS = 0
TIER_MP3_320 = 1
TIER_OTHER = 2

_REJECT_WORDS = {"discography", "anthology", "karaoke", "instrumental", "tribute"}


@dataclasses.dataclass
class Release:
    guid: str
    title: str
    size: int
    indexer_id: int
    download_url: str
    categories: list[int]
    grabs: int = 0

    @classmethod
    def from_api(cls, raw: dict) -> "Release":
        return cls(
            guid=raw.get("guid", ""),
            title=raw.get("title", ""),
            size=int(raw.get("size") or 0),
            indexer_id=int(raw.get("indexerId") or 0),
            download_url=raw.get("downloadUrl", ""),
            categories=[int(c.get("id", 0)) for c in raw.get("categories") or []],
            grabs=int(raw.get("grabs") or 0),
        )

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def words(text: str) -> list[str]:
    """Lowercase alphanumeric tokens; '&' and 'and' are treated alike."""
    return re.findall(r"[a-z0-9]+", text.lower().replace("&", " and "))


def clean_album(name: str) -> str:
    """Drop Spotify's edition suffixes, which release names rarely carry.

    "Abbey Road (Remastered 2009)" and "Rumours - Super Deluxe" both search as
    the bare title.
    """
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name)
    name = re.sub(r"\s+-\s+.*$", "", name)
    return name.strip() or name


def tier(release: Release) -> int:
    tokens = set(words(release.title))
    if LOSSLESS_CATEGORY in release.categories or tokens & {"flac", "alac", "lossless"}:
        return TIER_LOSSLESS
    if "320" in tokens or "320kbps" in tokens:
        return TIER_MP3_320
    return TIER_OTHER


def matches(release: Release, artist: str, album: str, tracks: int) -> bool:
    """True when the release title names this album and its size fits the track count."""
    title = set(words(release.title))
    album_words = [w for w in words(clean_album(album)) if w not in {"the", "a", "and"}]
    if not album_words or not set(album_words) <= title:
        return False
    artist_words = [w for w in words(artist) if w not in {"the", "a", "and"}]
    if artist.lower() not in ("various artists", "") and not set(artist_words) <= title:
        return False
    if title & _REJECT_WORDS and not set(words(album)) & _REJECT_WORDS:
        return False
    if tracks > 0 and not (tracks * MIN_BYTES_PER_TRACK <= release.size <= tracks * MAX_BYTES_PER_TRACK):
        return False
    return True


def rank(releases: list[Release], artist: str, album: str, tracks: int, blocklist: set[str]) -> list[Release]:
    """Matching releases, best first: tier, then grabs (a proxy for completion)."""
    candidates = [
        r for r in releases
        if r.guid not in blocklist and r.download_url and matches(r, artist, album, tracks)
    ]
    return sorted(candidates, key=lambda r: (tier(r), -r.grabs, r.size))


class Prowlarr:
    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: int = 60) -> None:
        self.url = (url or os.environ.get("PROWLARR_URL", "http://prowlarr.music.svc.cluster.local:9696")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("PROWLARR_API_KEY", "")
        self.timeout = timeout

    def search(self, query: str) -> list[Release]:
        """One search across the enabled Usenet indexers.  Costs one indexer API hit."""
        resp = requests.get(
            f"{self.url}/api/v1/search",
            params={"query": query, "categories": AUDIO_CATEGORY, "type": "search"},
            headers={"X-Api-Key": self.api_key},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [Release.from_api(r) for r in resp.json() if r.get("protocol") == "usenet"]

    def nzb_location(self, release: Release) -> str:
        """The indexer URL behind a release's download link.  Prowlarr counts this as a grab."""
        resp = requests.get(release.download_url, allow_redirects=False, timeout=self.timeout)
        location = resp.headers.get("Location", "")
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            raise RuntimeError(f"Prowlarr answered {resp.status_code} without a redirect for {release.title}")
        return location


class Sabnzbd:
    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: int = 30) -> None:
        self.url = (url or os.environ.get("SABNZBD_URL", "http://sabnzbd.music.svc.cluster.local:8080")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("SABNZBD_API_KEY", "")
        self.timeout = timeout

    def _api(self, **params) -> dict:
        resp = requests.get(
            f"{self.url}/api",
            params={**params, "apikey": self.api_key, "output": "json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def add_url(self, nzb_url: str, name: str) -> str:
        """Queue an NZB by URL in the album category.  Returns the nzo_id."""
        data = self._api(mode="addurl", name=nzb_url, nzbname=name, cat=SAB_CATEGORY)
        ids = data.get("nzo_ids") or []
        if not data.get("status") or not ids:
            raise RuntimeError(f"SABnzbd refused {name}: {data.get('error', data)}")
        return ids[0]

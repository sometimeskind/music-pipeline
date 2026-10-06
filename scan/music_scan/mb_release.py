"""The MusicBrainz release behind a Spotify album (#209).

Navidrome groups an album by ``musicbrainz_albumid`` first, so the canonical
Spotify tags only merge editions when every item of the album also carries the
same MusicBrainz album IDs, or none.  This finds that release, rungs in order:

1. ``[MB-URL]``  the release MusicBrainz links to ``open.spotify.com/album/<id>``;
2. ``[MB-ISRC]`` a release holding the album's tracks (a recording search per
   ISRC, up to :data:`ISRC_SAMPLE` of them) with Spotify's track count and title;
3. ``[MB-UPC]``  a release with the album's barcode (one Spotify album call; the
   playlist pages carry no UPC);
4. ``[MB-NONE]`` none: the caller clears the album-level MusicBrainz tags.

Results are cached per Spotify album in :data:`CACHE_FILE`.  That file is also
the queue: a run resolves at most *budget* albums (MusicBrainz allows one
request a second), and the rest stay pending, their MusicBrainz tags untouched,
until a later run.  A miss is retried after :data:`RETRY_DAYS`, so a link added
to MusicBrainz later (Harmony) is picked up.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_scan.backfill import MB_INTERVAL, MB_USER_AGENT

logger = logging.getLogger(__name__)

MB_API = "https://musicbrainz.org/ws/2/"
SPOTIFY_ALBUM_URL = "https://open.spotify.com/album/"
HARMONY_URL = "https://harmony.pulsewidth.org.uk/release?url=" + urllib.parse.quote(SPOTIFY_ALBUM_URL, safe="")
# On pipeline-state, next to .albums.json.
CACHE_FILE = Path("/root/Music/inbox/spotdl/.mb-releases.json")
RETRY_DAYS = 7
ISRC_SAMPLE = 3
TIMEOUT = 30
RETRY_503_SECONDS = 5

URL, ISRC, UPC, NONE = "url", "isrc", "upc", "none"

# Album-level tags written from the release, or cleared on a miss.  Track-level
# IDs (mb_trackid, mb_artistid, isrc) are never touched.
MB_ALBUM_FIELDS = (
    "mb_albumid", "mb_releasegroupid", "mb_albumartistid", "mb_albumartistids",
    "albumdisambig", "releasegroupdisambig",
)


@dataclasses.dataclass(frozen=True)
class SpotifyAlbum:
    """What the resolver needs to know about a Spotify album."""

    album_id: str
    name: str
    tracks_count: int
    isrcs: tuple[str, ...] = ()


def album_fields(release: dict | None) -> dict:
    """The item fields for a looked-up *release* (``release/<id>``), or the cleared ones."""
    if release is None:
        return {f: [] if f == "mb_albumartistids" else "" for f in MB_ALBUM_FIELDS}
    artists = [c["artist"]["id"] for c in release.get("artist-credit", []) if c.get("artist", {}).get("id")]
    group = release.get("release-group") or {}
    return {
        "mb_albumid": release["id"],
        "mb_releasegroupid": group.get("id", ""),
        "mb_albumartistid": artists[0] if artists else "",
        "mb_albumartistids": artists,
        "albumdisambig": release.get("disambiguation", ""),
        "releasegroupdisambig": group.get("disambiguation", ""),
    }


def _title_words(title: str) -> frozenset[str]:
    from music_fetch.usenet import clean_album, words  # noqa: PLC0415

    return frozenset(words(clean_album(title)))


def _digits(code: str) -> str:
    return code.lstrip("0")


class MusicBrainz:
    """Rate-limited MusicBrainz web service reads.  404 → None.

    A 503 is MusicBrainz's rate-limit answer: the request is retried once after
    *retry_after* seconds before the error reaches the caller (which leaves the
    album pending for the next run)."""

    def __init__(self, interval: float = MB_INTERVAL, retry_after: float = RETRY_503_SECONDS) -> None:
        self.interval = interval
        self.retry_after = retry_after
        self._last = 0.0
        self.calls = 0

    def get(self, path: str, **params: str) -> dict | None:
        url = MB_API + path + "?" + urllib.parse.urlencode({**params, "fmt": "json"})
        try:
            return self._get(url)
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise
            logger.info("  [MB-503] %s: MusicBrainz is busy; retrying in %gs", path, self.retry_after)
            time.sleep(self.retry_after)
            return self._get(url)

    def _get(self, url: str) -> dict | None:
        wait = self._last + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — fixed https host
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        finally:
            self._last = time.monotonic()
            self.calls += 1


def by_url(mb: MusicBrainz, album: SpotifyAlbum) -> str | None:
    data = mb.get("url", resource=SPOTIFY_ALBUM_URL + album.album_id, inc="release-rels")
    ids = sorted(
        r["release"]["id"] for r in (data or {}).get("relations", [])
        if r.get("target-type") == "release" and r.get("release", {}).get("id")
    )
    return ids[0] if ids else None


def _best(releases: Iterable[dict], album: SpotifyAlbum) -> str | None:
    """Of *releases* with the album's track count and title, the official
    digital one, then the earliest.  None when nothing qualifies."""
    want = _title_words(album.name)
    fits = [
        r for r in releases
        if r.get("track-count") == album.tracks_count and _title_words(r.get("title", "")) == want
    ]
    if not fits:
        return None

    def key(r: dict):
        digital = any(m.get("format") == "Digital Media" for m in r.get("media", []))
        return (r.get("status") != "Official", not digital, r.get("date") or "9999", r["id"])

    return min(fits, key=key)["id"]


def by_isrc(mb: MusicBrainz, album: SpotifyAlbum) -> str | None:
    """A release every sampled ISRC's recording appears on."""
    common: dict[str, dict] | None = None
    for code in album.isrcs[:ISRC_SAMPLE]:
        data = mb.get("recording", query=f"isrc:{code}", limit="25") or {}
        found = {r["id"]: r for rec in data.get("recordings", []) for r in rec.get("releases", [])}
        if not found:
            continue  # MusicBrainz doesn't know this ISRC; the others may still agree
        common = found if common is None else {k: v for k, v in common.items() if k in found}
        if not common:
            return None
    return _best(common.values(), album) if common else None


def by_upc(mb: MusicBrainz, album: SpotifyAlbum, upc_of: Callable[[str], str | None]) -> str | None:
    upc = upc_of(album.album_id)
    if not upc:
        return None
    data = mb.get("release", query=f"barcode:{upc} OR barcode:{_digits(upc)} OR barcode:0{_digits(upc)}") or {}
    hits = [r for r in data.get("releases", []) if _digits(r.get("barcode") or "") == _digits(upc)]
    if not hits:
        return None
    # A barcode is one product; prefer the matching track count when several share it.
    return _best(hits, album) or min(hits, key=lambda r: r["id"])["id"]


def spotify_upc(album_id: str) -> str | None:
    """The album's UPC from Spotify's album object.  One call, rate-limit guarded."""
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415
    from spotdl.utils.spotify import SpotifyClient  # noqa: PLC0415

    SpotifyPlaylists(ingest.COOKIE_FILE)  # initialises the shared client with the fail-fast adapter
    return (SpotifyClient().album(album_id).get("external_ids") or {}).get("upc")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Resolver:
    """Spotify album → album-level MusicBrainz fields, through the cache/queue."""

    def __init__(
        self,
        cache_file: Path | None = CACHE_FILE,
        budget: int | None = None,
        mb: MusicBrainz | None = None,
        upc_of: Callable[[str], str | None] = spotify_upc,
    ) -> None:
        self.cache_file = cache_file
        self.budget = budget
        self.mb = mb or MusicBrainz()
        self.upc_of = upc_of
        self.cache: dict[str, dict] = {}
        self.looked_up = 0
        self.rungs: dict[str, int] = {}
        if cache_file is not None:
            try:
                self.cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                logger.warning("Ignoring unreadable MusicBrainz release cache %s", cache_file)

    def save(self) -> None:
        if self.cache_file is None:
            return
        try:
            self.cache_file.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save the MusicBrainz release cache %s: %s", self.cache_file, exc)

    def _due(self, entry: dict | None) -> bool:
        if entry is None:
            return True
        if entry["rung"] != NONE:
            return False
        return datetime.fromisoformat(entry["checked"]) < _now() - timedelta(days=RETRY_DAYS)

    def fields(self, album: SpotifyAlbum) -> dict | None:
        """The album-level MusicBrainz fields for *album*'s items, or None while
        it is pending (not looked up yet, budget spent, or the lookup failed)."""
        entry = self.cache.get(album.album_id)
        if self._due(entry):
            if self.budget is not None and self.looked_up >= self.budget:
                return album_fields(None) if entry else None  # a stale miss stays a miss
            looked = self._look_up(album)
            if looked is None:
                return album_fields(None) if entry else None
            entry = self.cache[album.album_id] = looked
            self.rungs[entry["rung"]] = self.rungs.get(entry["rung"], 0) + 1
        return entry["fields"] if entry["rung"] != NONE else album_fields(None)

    def _look_up(self, album: SpotifyAlbum) -> dict | None:
        from music_fetch.spotify_limit import SpotifyRateLimited  # noqa: PLC0415

        self.looked_up += 1
        label = f"{album.name} ({album.album_id})"
        try:
            rung, release_id = URL, by_url(self.mb, album)
            if release_id is None:
                rung, release_id = ISRC, by_isrc(self.mb, album)
            if release_id is None:
                try:
                    rung, release_id = UPC, by_upc(self.mb, album, self.upc_of)
                except SpotifyRateLimited:
                    logger.warning("  [MB-WAIT] %s: Spotify is rate-limiting; barcode rung retried next run", label)
                    return None
            release = None
            if release_id is not None:
                release = self.mb.get(f"release/{release_id}", inc="artist-credits+release-groups")
        except Exception as exc:
            logger.warning("  [MB-WAIT] %s: MusicBrainz lookup failed, retried next run: %s", label, exc)
            return None
        checked = _now().replace(microsecond=0).isoformat()
        if release is None:
            logger.info("  [MB-NONE] %s: no MusicBrainz release; add it with %s%s",
                        label, HARMONY_URL, album.album_id)
            return {"rung": NONE, "checked": checked}
        logger.info("  [MB-%s] %s → %s", rung.upper(), label, release["id"])
        return {"rung": rung, "checked": checked, "fields": album_fields(release)}

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
import unicodedata

import requests

from music_fetch.redact import redact_text

logger = logging.getLogger(__name__)

AUDIO_CATEGORY = 3000
LOSSLESS_CATEGORY = 3040
SAB_CATEGORY = "album"

# Size sanity per album track: below this a release is a single or a sampler;
# above it, a discography or a video.  The fallback when the album's duration
# is unknown; the per-second bound runs from ~130 kbps lossy to 24/192 FLAC (#189).
MIN_BYTES_PER_TRACK = 1_000_000
MAX_BYTES_PER_TRACK = 150_000_000
MIN_BYTES_PER_SECOND = 16_000   # ~1 MB/min
MAX_BYTES_PER_SECOND = 1_000_000  # 60 MB/min

# Tiers, best first.  The library is AAC 256k: lossless transcodes cleanly,
# MP3 320 is kept as-is by beets (never_convert_lossy_files).
TIER_LOSSLESS = 0
TIER_MP3_320 = 1
TIER_OTHER = 2

_REJECT_WORDS = {"discography", "anthology", "karaoke", "instrumental", "tribute"}

# Release types shorter than an album; accepted only when Spotify calls it a single.
_SINGLE_WORDS = {"single", "ep", "cdm", "cds", "cdep", "maxi"}

# Words a release title may carry beyond artist and album: format, source,
# edition and release-type tags.  Anything else is a leftover word, a sign the
# release is a different album that contains this one's name (#189).
_TAG_WORDS = {
    "a", "an", "and", "the", "of", "va",
    "flac", "web", "dl", "cd", "cdda", "cdr", "vinyl", "lp", "mp3", "aac", "alac", "m4a", "ogg",
    "lossless", "hires", "hi", "res", "cbr", "vbr", "kbps", "bit", "khz", "mono", "stereo",
    "retail", "promo", "proper", "repack", "dirfix", "nfofix", "int",
    "remastered", "remaster", "deluxe", "edition", "expanded", "bonus", "tracks", "track",
    "reissue", "limited", "special", "anniversary", "version", "digital", "album", "explicit", "clean",
    "bandcamp", "qobuz", "tidal", "deezer", "itunes", "hdtracks", "mfit",
    "us", "uk", "eu", "jp", "de",
} | _SINGLE_WORDS
MAX_LEFTOVER_WORDS = 2
# Edition words that tell one edition of an album from another (#279), each
# folded to one spelling.  "edition" itself names none.
_EDITION_WORDS = {"deluxe": "deluxe", "expanded": "expanded", "anniversary": "anniversary",
                  "remaster": "remaster", "remastered": "remaster", "reissue": "reissue",
                  "special": "special", "bonus": "bonus", "limited": "limited"}
# Words a release name may add or drop around a name ("The Beatles", "Love & Devotion").
_ARTICLES = {"the", "a", "and"}


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


# Letters NFKD leaves whole; release names spell them out.
_FOLD = str.maketrans({"æ": "ae", "œ": "oe", "ø": "o", "ð": "d", "þ": "th", "ł": "l", "đ": "d", "ı": "i"})
_APOSTROPHES = re.compile(r"['’ʼ‘`]")


def _prepare(text: str) -> str:
    """Fold *text* the way release names spell a stylised name (#197).

    Diacritics go (Beyoncé → beyonce); '$' is always an s (WOR$T, Ke$ha); '@'
    and '!' only inside a word (P!nk, but Help!); '&' and '+' read 'and';
    apostrophes are deleted, joining the word (WHACK'S → whacks).
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().translate(_FOLD)
    text = _APOSTROPHES.sub("", text).replace("$", "s")
    text = re.sub(r"(?<=[a-z0-9])@(?=[a-z0-9])", "a", text)
    text = re.sub(r"(?<=[a-z0-9])!(?=[a-z0-9])", "i", text)
    return re.sub(r"[&+]", " and ", text)


def words(text: str) -> list[str]:
    """Lowercase ASCII alphanumeric tokens of *text*, folded by :func:`_prepare`.
    The query and the matcher share it, so both read WOR$T as worst."""
    return re.findall(r"[a-z0-9]+", _prepare(text))


def normalise(text: str) -> str:
    """*text* as search words: ``"Slayyyter WOR$T GIRL"`` → ``"slayyyter worst girl"``."""
    return " ".join(words(text))


def readable(text: str) -> bool:
    """True when most of *text* survives as words.  A name made of glyphs or
    Zalgo marks leaves a few stray letters (an ``l``, a ``v``), which would
    only search as noise; it needs the artist, or an override (#197)."""
    chars = [c for c in _prepare(text) if not c.isspace()]
    kept = sum(1 for c in chars if c.isascii() and c.isalnum())
    return kept > 0 and kept * 2 >= len(chars)


def clean_album(name: str) -> str:
    """Drop Spotify's edition suffixes, which release names rarely carry.

    "Abbey Road (Remastered 2009)" and "Rumours - Super Deluxe" both search as
    the bare title.
    """
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name)
    name = re.sub(r"\s+-\s+.*$", "", name)
    return name.strip() or name


def search_title(name: str) -> str:
    """An album name as album mode searches and matches it: :func:`clean_album`,
    and Spotify's bonus-track count dropped too ("Saint Cloud +3" would search
    as "saint cloud and 3", homelab#2568).  Not in clean_album, whose scan-side
    callers compare track titles, where a dropped word loosens a veto."""
    name = clean_album(name)
    return re.sub(r"\s+\+\s?\d+$", "", name).strip() or name


def tier(release: Release) -> int:
    tokens = set(words(release.title))
    if LOSSLESS_CATEGORY in release.categories or tokens & {"flac", "alac", "lossless"}:
        return TIER_LOSSLESS
    if "320" in tokens or "320kbps" in tokens:
        return TIER_MP3_320
    return TIER_OTHER


def _drop_group(title: str) -> str:
    """Strip the scene group, the last segment of a dash-separated title
    ("Artist-Album-WEB-FLAC-2024-ENRiCH").  A title with fewer than three
    segments keeps its last one: it may be the album."""
    segments = re.split(r"(?<! )-(?! )", title)
    return "-".join(segments[:-1]) if len(segments) >= 3 else title


def leftover(release: Release, artist: str, album: str) -> list[str]:
    """Title words that are not artist, album, a year or number, or a known tag.

    A word with a digit counts in the leading (artist) segment, where it is a
    name, not a year or catalogue number: ``Elvis27-Electronic-…`` (#200).
    """
    known = set(words(artist)) | set(words(album)) | _TAG_WORDS
    title = _drop_group(release.title)
    segments = re.split(r"(?<! )-(?! )", title)
    lead = set(words(segments[0])) if len(segments) >= 2 else set()
    return [w for w in words(title) if w not in known and (w in lead or not any(c.isdigit() for c in w))]


def edition(text: str) -> frozenset[str]:
    """The edition words of an album name or release title (#279):
    ``Ready to Die (The Remaster)`` → ``{remaster}``."""
    return frozenset(_EDITION_WORDS[w] for w in words(text) if w in _EDITION_WORDS)


def edition_miss(release: Release, album: str) -> int:
    """How far the release's edition is from the Spotify album's: edition
    words one names and the other doesn't.  0 is the same edition."""
    return len(edition(release.title) ^ edition(album))


def _leads_with_artist(title_words: list[str], artist: str, album: str) -> bool:
    """True when the title starts with the artist, as scene names do
    (``Artist-Album-…``); a collaboration may follow (``Artist X Other-…``).
    A compilation starts ``VA``/``Various``.  An override (no artist) starts
    with one of its own words (#200)."""
    lead = [w for w in title_words if w not in _ARTICLES]
    if not artist:
        return bool(lead) and lead[0] in set(words(album))
    if artist.lower() == "various artists":
        return lead[:1] in (["va"], ["various"])
    names = [w for w in words(artist) if w not in _ARTICLES]
    return set(lead[:len(names)]) == set(names)


def _album_follows_artist(title_words: list[str], artist: str, album: str) -> bool:
    """True when a self-titled album's name comes right after the artist, as a
    release names it (``The Meters-The Meters-…``).  Anywhere else in the title
    it is part of another album's name: ``The Meters-The Essentials The
    Meters-…`` is a compilation (homelab#2568).  Articles don't count."""
    lead = [w for w in title_words if w not in _ARTICLES]
    names = [w for w in words(artist) if w not in _ARTICLES]
    name = [w for w in words(search_title(album)) if w not in _ARTICLES]
    return bool(names) and lead[:len(names)] == names and lead[len(names):len(names) + len(name)] == name


@dataclasses.dataclass(frozen=True)
class Rejection:
    """Why a release is not the album (homelab#2568).  *stage* counts the rules
    it passed, so a higher one came closer; *distance* orders within a stage
    (album words lacking, leftover words).  *rule* is the short name a [MISS]
    tallies; *detail* says what failed."""
    stage: int
    rule: str
    detail: str = ""
    distance: int = 0

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}" if self.detail else self.rule


def rejection(
    release: Release,
    artist: str,
    album: str,
    tracks: int,
    *,
    album_type: str | None = None,
    seconds: int = 0,
    any_album: bool = False,
) -> Rejection | None:
    """None when the release title names this album, carries little else, and
    its size fits the album's duration (or, unknown, its track count); else the
    first rule it fails.

    *any_album* is for a title with no readable words (#197): any release by
    the artist matches on size and type alone, and the pick is logged as such.
    """
    title_words = words(release.title)
    title = set(title_words)
    album_words = [w for w in words(search_title(album)) if w not in _ARTICLES]
    if not any_album:
        if not album_words:
            return Rejection(0, "album words", "the album has none")
        lacking = [w for w in dict.fromkeys(album_words) if w not in title]
        if lacking:
            return Rejection(0, "album words", "lacks " + " ".join(lacking), distance=len(lacking))
    artist_words = [w for w in words(artist) if w not in _ARTICLES]
    if not _leads_with_artist(title_words, artist, album):
        return Rejection(1, "artist", "doesn't start with " + (artist or "an album word"))
    if title & _REJECT_WORDS and not set(words(album)) & _REJECT_WORDS:
        return Rejection(2, "reject word", " ".join(sorted(title & _REJECT_WORDS)))
    extra = [] if any_album else leftover(release, artist, album)
    if len(extra) > MAX_LEFTOVER_WORDS:
        return Rejection(3, "leftover words", " ".join(extra), distance=len(extra))
    # Self-titled: the artist words already satisfy the album check, so the
    # title must name the album again right after the artist, say so, or
    # carry nothing else.
    if not any_album and set(album_words) <= set(artist_words) and not (
        _album_follows_artist(title_words, artist, album)
        or {"self", "titled"} <= title
        or not extra
    ):
        return Rejection(4, "self-titled", "names the album once, beside " + " ".join(extra))
    is_single = album_type == "single" if album_type else 0 < tracks <= 3
    shorter = sorted(title & _SINGLE_WORDS - set(words(album)))
    if shorter and not is_single:
        return Rejection(5, "single", f"tagged {' '.join(shorter)}, the album isn't one")
    if seconds > 0:
        low, high = seconds * MIN_BYTES_PER_SECOND, seconds * MAX_BYTES_PER_SECOND
    elif tracks > 0:
        low, high = tracks * MIN_BYTES_PER_TRACK, tracks * MAX_BYTES_PER_TRACK
    else:
        low = high = 0
    if high and not low <= release.size <= high:
        return Rejection(6, "size", f"{release.size / 1e6:.0f} MB, not {low / 1e6:.0f}-{high / 1e6:.0f} MB")
    return None


def matches(release: Release, artist: str, album: str, tracks: int, **kwargs) -> bool:
    """True when :func:`rejection` finds no rule the release fails."""
    return rejection(release, artist, album, tracks, **kwargs) is None


def why_none(
    releases: list[Release],
    artist: str,
    album: str,
    tracks: int,
    blocklist: set[str],
    *,
    album_type: str | None = None,
    seconds: int = 0,
    any_album: bool = False,
    shown: int = 3,
) -> str:
    """Why no release matched, for a [MISS] (homelab#2568): how many failed each
    rule, then the *shown* closest titles and the rule each failed, so a miss
    can be judged from the log.  A release that fails no rule but is
    blocklisted (or has no download link) counts as the closest."""
    found: list[tuple[Release, Rejection]] = []
    for r in releases:
        why = rejection(r, artist, album, tracks, album_type=album_type, seconds=seconds, any_album=any_album)
        if why is None:
            why = Rejection(7, "blocklisted") if r.guid in blocklist else Rejection(7, "no download link")
        found.append((r, why))
    if not found:
        return ""
    tally: dict[str, int] = {}
    for _, why in found:
        tally[why.rule] = tally.get(why.rule, 0) + 1
    counts = ", ".join(f"{n} {rule}" for rule, n in sorted(tally.items(), key=lambda kv: -kv[1]))
    found.sort(key=lambda rw: (-rw[1].stage, rw[1].distance, -rw[0].grabs))
    closest = "; ".join(f'"{r.title}" ({why})' for r, why in found[:shown])
    return f"rejected: {counts}; closest: {closest}"


def rank(
    releases: list[Release],
    artist: str,
    album: str,
    tracks: int,
    blocklist: set[str],
    year: int | str | None = None,
    *,
    album_type: str | None = None,
    seconds: int = 0,
    any_album: bool = False,
) -> list[Release]:
    """Matching releases, best first: the Spotify album's edition (#279: its
    deluxe, remaster or anniversary words, and no others), then tier, then
    fewest leftover title words, then the Spotify year in the title (a remaster
    or reissue usually carries a different one), then grabs (a proxy for
    completion).  Another edition still matches: it is the album when Usenet
    has no copy of Spotify's."""
    candidates = [
        r for r in releases
        if r.guid not in blocklist and r.download_url
        and matches(r, artist, album, tracks, album_type=album_type, seconds=seconds, any_album=any_album)
    ]
    year_word = str(year) if year else None

    def other_year(r: Release) -> int:
        return 0 if year_word is None or year_word in words(r.title) else 1

    return sorted(candidates, key=lambda r: (edition_miss(r, album), tier(r), len(leftover(r, artist, album)),
                                             other_year(r), -r.grabs, r.size))


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

    def blocked(self) -> list[dict]:
        """Indexers in Prowlarr's failure backoff (#263), as ``{indexerId,
        disabledTill, …}``.  Prowlarr lists only those whose ``disabledTill`` is
        in the future, and meanwhile answers searches with an instant empty list.
        Costs no indexer API hit."""
        resp = requests.get(
            f"{self.url}/api/v1/indexerstatus",
            headers={"X-Api-Key": self.api_key},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def nzb_location(self, release: Release) -> str:
        """The indexer URL behind a release's download link.  Prowlarr counts this as a grab."""
        try:
            resp = requests.get(release.download_url, allow_redirects=False, timeout=self.timeout)
        except requests.RequestException as exc:
            # requests names the URL, API key and all; the caller logs this (#343).
            raise RuntimeError(redact_text(f"Prowlarr download link for {release.title}: {exc}")) from None
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

    def finished(self, nzo_ids: list[str]) -> dict[str, dict]:
        """History entries for *nzo_ids* that SABnzbd has finished, by nzo_id.

        Each value has ``ok`` (bool), ``storage`` (the job's dir in SABnzbd's
        pod) and ``fail_message``.  Jobs still queued or post-processing are
        absent.
        """
        if not nzo_ids:
            return {}
        slots = self._api(mode="history", nzo_ids=",".join(nzo_ids)).get("history", {}).get("slots", [])
        return {
            slot["nzo_id"]: {
                "ok": slot.get("status") == "Completed",
                "storage": slot.get("storage") or "",
                "fail_message": slot.get("fail_message") or "",
            }
            for slot in slots
            if slot.get("status") in ("Completed", "Failed")
        }

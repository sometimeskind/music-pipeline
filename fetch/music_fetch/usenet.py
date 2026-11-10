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
# Leftover words that make the title another release holding the album's name
# (homelab#2604): "The Born in the U.S.A. Tour 84-85" is a live box set, not
# Born in the U.S.A.  Only as leftovers: a word of the album's own name
# ("Hysteria (Live)") or the artist's (the band Live) is the album's.  Each
# names a recording or a collection other than the album; edition words
# (deluxe, expanded) stay tags, so an edition of the album still matches.
_OTHER_RELEASE_WORDS = {"tour", "live", "concert", "concerts", "unplugged", "bootleg", "bootlegs",
                        "box", "boxset", "acoustic", "remixes", "remixed"}
# What a compilation's release may add to its name without being another album
# (homelab#2656): "The Definitive Groove Collection" is Definitive Groove: Sister
# Sledge.  Only for :func:`named_by_artist`; elsewhere it stays a leftover.
_COLLECTION_WORDS = {"collection"}
# Edition words that tell one edition of an album from another (#279), each
# folded to one spelling.  "edition" itself names none.
_EDITION_WORDS = {"deluxe": "deluxe", "expanded": "expanded", "anniversary": "anniversary",
                  "remaster": "remaster", "remastered": "remaster", "reissue": "reissue",
                  "special": "special", "bonus": "bonus", "limited": "limited"}
# Words a release name may add or drop around a name ("The Beatles", "Love & Devotion").
_ARTICLES = {"the", "a", "and"}
# A subtitle holding one of these describes the album rather than naming it
# (homelab#2662): "Super Deluxe" is an edition, and "Original Motion Picture
# Soundtrack" has "OST" for its release.
_NOTE_WORDS = set(_EDITION_WORDS) | {"soundtrack", "ost", "motion", "picture", "film", "movie", "series", "score"}
_DECADE = re.compile(r"(?:19|20)?\d0s")
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_SUBTITLE = re.compile(r"\s+-\s+(.*)$")


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


def exclusion(text: str) -> str:
    """Why search words can't be used, when one is written as an exclusion
    (``-Son``); else empty.  Nothing excludes: :func:`words` drops the dash, so
    ``-Son`` would be a word every title must hold, refusing the album it
    was meant to find.  A dash inside a word (Jay-Z) or on its own is left alone."""
    dashed = [w for w in (t.strip("\"'“”‘’()") for t in text.split()) if w.startswith("-") and words(w)]
    if not dashed:
        return ""
    return (f"{' '.join(dashed)}: there is no exclusion syntax, a word starting with - would be a word every "
            "release title must hold; drop it, and pick the edition with words its title does hold")


def readable(text: str) -> bool:
    """True when most of *text* survives as words.  A name made of glyphs or
    Zalgo marks leaves a few stray letters (an ``l``, a ``v``), which would
    only search as noise; it needs the artist, or an override (#197)."""
    chars = [c for c in _prepare(text) if not c.isspace()]
    kept = sum(1 for c in chars if c.isascii() and c.isalnum())
    return kept > 0 and kept * 2 >= len(chars)


def latin(text: str) -> bool:
    """True when :func:`words` keeps every letter of *text*.  One it drops is
    another script (Кино, 坂本龍一): the words left can't speak for the name."""
    return not any(c.isalpha() and not c.isascii() for c in _prepare(text))


def leading_word(name: str) -> str:
    """The word a release title naming *name* starts with: its first but an article."""
    return next((w for w in words(name) if w not in _ARTICLES), "")


def clean_album(name: str) -> str:
    """Drop Spotify's edition suffixes, which release names rarely carry.

    "Abbey Road (Remastered 2009)" and "Rumours - Super Deluxe" both search as
    the bare title.
    """
    name = _SUBTITLE.sub("", _BRACKETS.sub("", name))
    return name.strip() or name


def subtitle(name: str) -> str:
    """What :func:`clean_album` drops after ``" - "``: ``"The Very Best of Aretha
    Franklin - The 70's"`` → ``"The 70's"``."""
    found = _SUBTITLE.search(_BRACKETS.sub("", name))
    return found[1] if found else ""


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


def joined(album: str) -> str:
    """The album's name with its words run together, as a scene title may write
    it (homelab#2754): ``Getz/Gilberto`` → ``getzgilberto``.  The words are the
    album-name check's, in order and without articles; empty for a name of one
    word, which has nothing to join."""
    name = [w for w in words(search_title(album)) if w not in _ARTICLES]
    return "".join(name) if len(name) >= 2 else ""


def _names_joined(title_words: list[str], artist: str, album: str) -> bool:
    """True when the title writes the album's name as one word (:func:`joined`)
    that isn't the artist's: ``Stan Getz-GetzGilberto-…``.  A word the artist's
    name holds names the artist, not the album (homelab#2656)."""
    one = joined(album)
    return bool(one) and one in title_words and one not in words(artist)


def leftover(release: Release, artist: str, album: str) -> list[str]:
    """Title words that are not artist, album, a year or number, or a known tag.

    A word with a digit counts in the leading (artist) segment, where it is a
    name, not a year or catalogue number: ``Elvis27-Electronic-…`` (#200).  The
    album's words run together are its name, not a leftover (homelab#2754).
    """
    known = set(words(artist)) | set(words(album)) | {joined(album)} | _TAG_WORDS
    title = _drop_group(release.title)
    segments = re.split(r"(?<! )-(?! )", title)
    lead = set(words(segments[0])) if len(segments) >= 2 else set()
    return [w for w in words(title) if w not in known and (w in lead or not any(c.isdigit() for c in w))]


def other_release(release: Release, artist: str, album: str) -> list[str]:
    """Leftover title words that name another release (homelab#2604):
    ``Artist-The Album Tour 84-85-…`` → ``["tour"]``.  One followed by
    "edition" names an edition of the album ("Special Tour Edition", a
    reissue with bonus tracks), so it doesn't count."""
    other = set(leftover(release, artist, album)) & _OTHER_RELEASE_WORDS
    if not other:
        return []
    title_words = words(_drop_group(release.title))
    return list(dict.fromkeys(
        w for i, w in enumerate(title_words)
        if w in other and title_words[i + 1:i + 2] != ["edition"]
    ))


def edition(text: str) -> frozenset[str]:
    """The edition words of an album name or release title (#279):
    ``Ready to Die (The Remaster)`` → ``{remaster}``."""
    return frozenset(_EDITION_WORDS[w] for w in words(text) if w in _EDITION_WORDS)


# What an indexer appends to a repost's name: ``-1``, ``.1``, ``-FTP`` (homelab#2593).
_REPOST_SUFFIX = re.compile(r"(?:[-.][1-9]|-ftp)$", re.IGNORECASE)


def repost_name(title: str) -> str:
    """A release title without the suffix an indexer gives a repost of it
    (homelab#2593): ``Artist-Album-FLAC-PERFECT-1`` → ``artist-album-flac-perfect``.
    Only one suffix goes, and only those: a year (``-2006``) or a REPACK stays."""
    return _REPOST_SUFFIX.sub("", title.strip()).lower()


@dataclasses.dataclass
class Copies:
    """Releases known by GUID and, for some, by :func:`repost_name` of their
    title (homelab#2757).  Each indexer gives a post its own GUID, and with two
    indexers in Prowlarr one release comes back under two, so a release is one
    of these when its GUID is or its repost name is, whichever indexer returned
    it.  By title alone, not size: a release title names its group and source,
    and the indexers needn't count a post's size alike."""
    guids: set[str] = dataclasses.field(default_factory=set)
    names: set[str] = dataclasses.field(default_factory=set)

    def add(self, guid: str | None, title: str | None = None) -> None:
        """Add a release by its *guid*, and with a *title* every copy of it."""
        if guid:
            self.guids.add(guid)
        if title and (name := repost_name(title)):
            self.names.add(name)

    def __contains__(self, release: object) -> bool:
        if not isinstance(release, Release):
            return False
        return release.guid in self.guids or bool(release.title) and repost_name(release.title) in self.names

    def __or__(self, other: Copies) -> Copies:
        return Copies(self.guids | other.guids, self.names | other.names)


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


def named_by_artist(release: Release, artist: str, album: str) -> str:
    """What the title adds to an album name it holds only through the artist's
    (homelab#2656), as "<album words>, beside <leftover words>"; empty when it
    doesn't.  ``Curtis Mayfield-Live In Europe-…`` holds *Curtis Live!* only
    because the artist is called Curtis: past the artist's name in the title
    there is no "curtis", and "in europe" says it is another album.

    The name is often left out of the album's (``Toto-IV-…``, ``Canned Heat-The
    Best Of-…``), so a word lacking there alone says nothing: as for a
    self-titled album, the title must then carry nothing else.  The artist's
    name is Spotify's, read off the title's start, so more people named there
    (``Stan Getz and Joao Gilberto-Getz Gilberto-…``) and titles with no hyphen
    work alike, and so does the album's name with its words run together
    (``…-GetzGilberto-…``, homelab#2754).  A self-titled album has its own
    rule, and an override no artist.
    """
    names = [w for w in words(artist) if w not in _ARTICLES]
    album_words = [w for w in words(search_title(album)) if w not in _ARTICLES]
    lead = [w for w in words(release.title) if w not in _ARTICLES]
    if not names or set(album_words) <= set(names) or set(lead[:len(names)]) != set(names):
        return ""
    rest = lead[len(names):]
    if _names_joined(rest, artist, album):
        return ""
    only = [w for w in dict.fromkeys(album_words) if w in names and w not in rest]
    extra = [w for w in leftover(release, artist, album) if w not in _COLLECTION_WORDS]
    return f"{' '.join(only)}, beside {' '.join(extra)}" if only and extra else ""


def decades(text: str) -> set[str]:
    """The decades *text* names, whatever the century: ``The '60s`` and ``1960s`` → ``{"60s"}``."""
    return {w[-3:] for w in words(text) if _DECADE.fullmatch(w)}


def subtitle_clash(release: Release, artist: str, album: str) -> str:
    """How the title says it is another album than the one the subtitle names
    (homelab#2662), which the album-name check can't see: it reads the name
    without its subtitle.  Empty when it doesn't.  ``Aretha Franklin-The Very Best
    Of Aretha Franklin The 60s-…`` holds every word of *The Very Best of Aretha
    Franklin - The 70's* it is asked for, and it is another volume.

    Two things say so: a decade other than the album's, with none of the album's;
    or a title with none of the subtitle's own words and with leftover words
    instead.  A title that only leaves the subtitle out is the album.  A subtitle
    with a note word (``Rumours - Super Deluxe``) names nothing to miss; tags and
    the name's own words aren't its own, nor are numbers and years, which titles
    hold for other reasons.  A leftover word in the artist's segment says nothing
    about the album: ``Prince And The Revolution-Parade-…``.
    """
    wanted, found = decades(album), decades(release.title)
    if wanted and found and not wanted & found:
        return f"{' '.join(sorted(found))}, not {' '.join(sorted(wanted))}"
    note = words(subtitle(album))
    known = set(words(artist)) | set(words(search_title(album))) | _TAG_WORDS
    own = [w for w in dict.fromkeys(note)
           if w not in known and (_DECADE.fullmatch(w) or not any(c.isdigit() for c in w))]
    if not own or set(note) & _NOTE_WORDS or set(own) & set(words(release.title)):
        return ""
    segments = re.split(r"(?<! )-(?! )", _drop_group(release.title))
    lead = set(words(segments[0])) if len(segments) >= 2 else set()
    extra = [w for w in leftover(release, artist, album) if w not in lead]
    return f"lacks {' '.join(own)}, beside {' '.join(extra)}" if extra else ""


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


def size_range(tracks: int, seconds: int = 0) -> tuple[int, int]:
    """The bytes a release of the album may hold, by its duration or, unknown,
    its track count; ``(0, 0)`` with neither, when size is no rule."""
    if seconds > 0:
        return seconds * MIN_BYTES_PER_SECOND, seconds * MAX_BYTES_PER_SECOND
    if tracks > 0:
        return tracks * MIN_BYTES_PER_TRACK, tracks * MAX_BYTES_PER_TRACK
    return 0, 0


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
        # The name run together holds every word (homelab#2754).
        if lacking and not _names_joined(title_words, artist, album):
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
    borrowed = "" if any_album else named_by_artist(release, artist, album)
    if borrowed:
        return Rejection(5, "artist words", borrowed)
    other = [] if any_album else other_release(release, artist, album)
    if other:
        return Rejection(6, "other release", " ".join(other))
    clash = "" if any_album else subtitle_clash(release, artist, album)
    if clash:
        return Rejection(7, "subtitle", clash)
    is_single = album_type == "single" if album_type else 0 < tracks <= 3
    shorter = sorted(title & _SINGLE_WORDS - set(words(album)))
    if shorter and not is_single:
        return Rejection(8, "single", f"tagged {' '.join(shorter)}, the album isn't one")
    low, high = size_range(tracks, seconds)
    if high and not low <= release.size <= high:
        return Rejection(9, "size", f"{release.size / 1e6:.0f} MB, not {low / 1e6:.0f}-{high / 1e6:.0f} MB")
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
            why = Rejection(10, "blocklisted") if r.guid in blocklist else Rejection(10, "no download link")
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
    has no copy of Spotify's.

    The copies of one release (one :func:`repost_name`, homelab#2757) rank
    together, where the best of them would, and among themselves by indexer
    order first: the lowest Prowlarr indexer ID, the indexer added first
    (NinjaCentral; homelab's configure-prowlarr adds NZBNest after it).  Each
    indexer counts only its own grabs, so grabs don't compare across them.
    The GUID breaks the last tie, so the order results arrive in decides
    nothing."""
    candidates = [
        r for r in releases
        if r.guid not in blocklist and r.download_url
        and matches(r, artist, album, tracks, album_type=album_type, seconds=seconds, any_album=any_album)
    ]
    year_word = str(year) if year else None

    def other_year(r: Release) -> int:
        return 0 if year_word is None or year_word in words(r.title) else 1

    ranked = sorted(candidates, key=lambda r: (edition_miss(r, album), tier(r), len(leftover(r, artist, album)),
                                               other_year(r), -r.grabs, r.size, r.indexer_id, r.guid))
    first: dict[str, int] = {}
    for i, r in enumerate(ranked):
        first.setdefault(repost_name(r.title), i)
    return sorted(ranked, key=lambda r: (first[repost_name(r.title)], r.indexer_id))


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
        try:
            resp = requests.get(
                f"{self.url}/api",
                params={**params, "apikey": self.api_key, "output": "json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            # requests names the URL, API key and all; the caller logs this (homelab#2631).
            raise RuntimeError(redact_text(f"SABnzbd {params.get('mode')}: {exc}")) from None

    def add_url(self, nzb_url: str, name: str) -> str:
        """Queue an NZB by URL in the album category.  Returns the nzo_id."""
        data = self._api(mode="addurl", name=nzb_url, nzbname=name, cat=SAB_CATEGORY)
        ids = data.get("nzo_ids") or []
        if not data.get("status") or not ids:
            raise RuntimeError(f"SABnzbd refused {name}: {data.get('error', data)}")
        return ids[0]

    def delete_failed(self, nzo_id: str) -> None:
        """Delete a failed job from history, with its files: SABnzbd keeps a
        failed job's download in its incomplete dir until then (homelab#2631)."""
        self._api(mode="history", name="delete", value=nzo_id, del_files=1, archive=0)

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

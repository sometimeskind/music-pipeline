"""Track identity: Spotify track IDs and ISRCs carried on beets items (#176).

A Spotify track ID belongs to a playlist entry, not to a file: one recording has
different track IDs on the single, the album and compilations.  So an item
holds several in the ``spotify_ids`` flex attr, a comma list like ``sources``.
``isrc`` is beets' own field: every ISRC of the recording joined by ``;``, the
union of MusicBrainz's and Spotify's (they can disagree).

Identity ladder, first hit wins: Spotify track ID, ISRC, MusicBrainz recording
ID, then title+artist words as a last resort that callers log.  The words rung
only reaches items with no Spotify ID (#257): an item that has one is that
track, so a same-title entry with another ID is another recording.

Whether the library holds a recording (:class:`Recordings`, #287) takes the
ID rungs only: a copy leaves the library, or is deleted from
``quarantine/replaced/``, only when one of them agrees, or when a merge
recorded the file on a keeper whose title and artist its tags share (#303,
homelab#2539).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import NamedTuple

from music_fetch.usenet import clean_album
from music_fetch.usenet import words as release_words

_STOP_WORDS = frozenset({"the", "and", "for", "feat", "ft", "vs", "with", "a", "an", "of", "in", "on"})

# Ladder rungs, as ItemIndex.match reports them.
BY_ID, BY_ISRC, BY_WORDS = "id", "isrc", "words"

SPOTIFY_TRACK_URL = "https://open.spotify.com/track/"


def spotify_id(url: str | None) -> str | None:
    """The track ID in a Spotify track URL (query string dropped), or None."""
    if not url or not url.startswith(SPOTIFY_TRACK_URL):
        return None
    return url[len(SPOTIFY_TRACK_URL):].split("?")[0].strip("/") or None


def name_words(s: str) -> frozenset[str]:
    """Normalise a track/filename string to a set of significant lowercase words."""
    words = re.sub(r"[^\w\s]", " ", s.lower()).split()
    return frozenset(w for w in words if len(w) > 2 and w not in _STOP_WORDS)


def item_words(item) -> frozenset[str]:
    return name_words(f"{item.title or ''} {item.artist or item.albumartist or ''}")


def split_list(value: str | None, sep: str = ",") -> list[str]:
    """Non-empty, stripped entries of a separated flex attr."""
    return [p.strip() for p in (value or "").split(sep) if p.strip()]


MERGED_FILES = "merged_files"
"""Flex attr: the names of the files merges moved from this item's losers to
``quarantine/replaced/``, ``/``-separated (no file name holds one, #303)."""


def merged_files(item) -> list[str]:
    """:data:`MERGED_FILES` of *item*."""
    return split_list(item.get(MERGED_FILES), "/")


def add_merged_file(item, name: str) -> None:
    """Record *name* in *item*'s :data:`MERGED_FILES`."""
    if name and name not in (names := merged_files(item)):
        item[MERGED_FILES] = "/".join([*names, name])


def add_to_list(item, field: str, value: str | None) -> bool:
    """Append *value* to the comma list in *item*[*field*]. True when it changed."""
    if not value:
        return False
    entries = split_list(item.get(field))
    if value in entries:
        return False
    item[field] = ",".join(entries + [value])
    return True


class PlaylistTrack(NamedTuple):
    """One entry of an album record's per-playlist track list in the album store.

    Stored as a list: ``[name, artist, song_id, isrc, disc, track]``.  Records
    written before #176 hold only ``[name, artist]``.
    """

    name: str
    artist: str
    song_id: str | None = None
    isrc: str | None = None
    disc: int | None = None
    track: int | None = None

    @classmethod
    def from_entry(cls, entry: list) -> "PlaylistTrack":
        return cls(*entry[:6])


def item_spotify_ids(item) -> set[str]:
    """``spotify_ids``, plus the ID in ``spotify_url`` for items imported before #176."""
    ids = set(split_list(item.get("spotify_ids")))
    if sid := spotify_id(item.get("spotify_url")):
        ids.add(sid)
    return ids


def fingerprint(path) -> str:
    """The AcoustID fingerprint of the file at *path*, as beets' ``acoustid_fingerprint`` stores it.

    Computed locally (fpcalc/libchromaprint), no AcoustID lookup.  Evidence for
    the duplicate audit (#210); raises when the file can't be decoded.
    """
    import acoustid  # noqa: PLC0415

    _, fp = acoustid.fingerprint_file(str(path))
    return fp.decode() if isinstance(fp, bytes) else fp


def item_isrcs(item) -> set[str]:
    return set(split_list(item.get("isrc"), ";"))


def add_isrcs(item, codes) -> bool:
    """Union *codes* into the item's ``;``-joined ISRCs. True when it changed."""
    have = split_list(item.get("isrc"), ";")
    new = [c for c in dict.fromkeys(codes) if c and c not in have]
    if not new:
        return False
    item["isrc"] = ";".join(have + new)
    return True


class ItemIndex:
    """Library items indexed for the identity ladder; ``rungs`` counts the hits."""

    def __init__(self, items, identified_words: bool = False) -> None:
        """*identified_words* lets the words rung reach items that have a
        Spotify ID too: only for the backfill's wrong-version report, which
        looks for exactly those (#176)."""
        self.items = list(items)
        self.by_id: dict[str, object] = {}
        self.by_isrc: dict[str, object] = {}
        self.by_words: dict[frozenset, object] = {}
        for item in self.items:
            for sid in item_spotify_ids(item):
                self.by_id.setdefault(sid, item)
            for code in item_isrcs(item):
                self.by_isrc.setdefault(code, item)
            # An identified item is never another entry by words (#257).
            if (identified_words or not item_spotify_ids(item)) and (words := item_words(item)):
                self.by_words.setdefault(words, item)
        self.rungs: Counter = Counter()

    def match(self, song_id: str | None, isrc: str | None, name: str = "", artist: str = "", words: bool = True):
        """``(item, rung)`` for a playlist entry, or ``(None, None)``."""
        for rung, index, key in ((BY_ID, self.by_id, song_id), (BY_ISRC, self.by_isrc, isrc)):
            if key and key in index:
                self.rungs[rung] += 1
                return index[key], rung
        if words and (key := name_words(f"{name} {artist}")) and key in self.by_words:
            self.rungs[BY_WORDS] += 1
            return self.by_words[key], BY_WORDS
        return None, None

    def match_track(self, track: "PlaylistTrack", words: bool = True):
        return self.match(track.song_id, track.isrc, track.name, track.artist, words)

    def match_song(self, song: dict, words: bool = True):
        """Match a playlist song entry (spotdl's song format)."""
        return self.match(
            song.get("song_id") or spotify_id(song.get("url")), song.get("isrc"),
            song.get("name", ""), (song.get("artists") or [""])[0], words,
        )


def release_match(name: str, artist: str, items):
    """The first of *items* (one release's fresh tracks) that is the entry
    *name* by *artist*: the same title words, and every word of *artist* in
    the item's artist or album artist credit, or None (#240).

    Looser than the words rung, which needs the same title+artist word set:
    a release credits ``CFCF feat. nuum & Seren Forever`` where Spotify says
    ``CFCF``, and writes ``Marvin’s`` where Spotify says ``Marvins``.  Words
    are folded like release names (apostrophes joined, diacritics dropped).
    A remaster note counts (#252): ``Miserabilia (2018 Remaster)`` is another
    master than ``Miserabilia``.  Only for a release's own items, so a
    featured artist can't pull in an unrelated track."""
    title = set(release_words(name))
    credit = set(release_words(artist))
    if not title:
        return None
    for item in items:
        if (set(release_words(item.title or "")) == title
                and credit <= set(release_words(f"{item.artist or ''} {item.albumartist or ''}"))):
            return item
    return None


def same_base_title(name: str, title: str) -> bool:
    """True when Spotify's *name* and an item's *title* are one title once
    edition notes are dropped: any bracketed note and any `` - `` suffix, as
    :func:`music_fetch.usenet.clean_album` drops them from album names.
    ``Love - Remastered 2012`` is ``Love (Remastered 2012)``, never ``Bodies
    (remastered 2012)``.  Only a check on a match some number already made
    (#278): it never matches two recordings by itself."""
    base = release_words(clean_album(name))
    return bool(base) and base == release_words(clean_album(title))


def different_song(a: str, b: str) -> bool:
    """True when titles *a* and *b* name two songs once version notes are
    dropped as :func:`same_base_title` drops them (homelab#2544): ``Romance in
    Durango (Live at …)`` is not ``It Ain't Me, Babe (Live at …)``, while
    ``Oh! Sweet Nuthin'`` and ``Oh! Sweet Nuthin' (5.1 mix)`` are one song.
    A title with no words left (another script, glyphs only) never differs:
    there is nothing to tell the songs apart by."""
    x, y = release_words(clean_album(a or "")), release_words(clean_album(b or ""))
    return bool(x and y) and x != y


LENGTH_SLACK = 2.0
"""Seconds: two copies of one recording are at most this far apart in length.
An AcoustID match (:func:`music_scan.dupes.audio_match`) and a shared
MusicBrainz recording ID (:func:`same_mb_recording`) count only within it."""


def lengths_agree(a, b) -> bool:
    """True when lengths *a* and *b* (seconds) are within :data:`LENGTH_SLACK`,
    or either is unknown (nothing to veto with)."""
    try:
        a, b = float(a or 0), float(b or 0)
    except (TypeError, ValueError):
        return True
    return not (a and b) or abs(a - b) <= LENGTH_SLACK


def same_mb_recording(a_mbid: str | None, a_length, b_mbid: str | None, b_length) -> bool:
    """True when two items' ``mb_trackid`` agree and so do their lengths (homelab#2540).

    beets imports every file as a singleton and names its recording from title,
    artist and length, so an ``mb_trackid`` is a best guess: a sibling recording
    (the 5.1 mix of a stereo track, a live take of a studio one) passes the
    threshold.  The sibling's length gives it away; a recording's copies agree
    within :data:`LENGTH_SLACK`."""
    return bool(a_mbid) and a_mbid == b_mbid and lengths_agree(a_length, b_length)


def recording_ids(spotify_ids=(), isrcs=(), mb_trackid: str | None = None) -> frozenset[str]:
    """The identity keys of one recording (#287): ``spotify:<track ID>``,
    ``isrc:<code>`` and ``mb:<MusicBrainz recording ID>``.  Two recordings are
    one track only when a key agrees; titles never count (homelab#1889)."""
    keys = {f"spotify:{s.strip()}" for s in spotify_ids if s and s.strip()}
    keys |= {f"isrc:{c.strip().upper()}" for c in isrcs if c and c.strip()}
    if mb_trackid and mb_trackid.strip():
        keys.add(f"mb:{mb_trackid.strip().lower()}")
    return frozenset(keys)


def item_recording_ids(item) -> frozenset[str]:
    """:func:`recording_ids` of a beets item."""
    return recording_ids(item_spotify_ids(item), item_isrcs(item), item.get("mb_trackid"))


class MergeKeeper(NamedTuple):
    """An item whose :data:`MERGED_FILES` name a replaced file, as
    :class:`Recordings` weighs the name (homelab#2539)."""

    id: int
    title: str
    artist: str


def merge_agrees(file_title: str, file_artist: str, keeper_title: str, keeper_artist: str) -> bool:
    """True when a merged file's tags plausibly name its keeper's recording
    (homelab#2539): the same title once edition notes are dropped
    (:func:`same_base_title`), and artists that share a word when both are
    known.  An untitled file never agrees: nothing says the merge was right.
    Only a check on the merge's decision, never identity by itself."""
    if not same_base_title(file_title or "", keeper_title or ""):
        return False
    a, b = set(release_words(file_artist or "")), set(release_words(keeper_artist or ""))
    return not (a and b) or not a.isdisjoint(b)


class Recordings:
    """The recordings some items hold, by :func:`recording_ids` (#287).

    The one place that says whether "the library holds this recording": a
    copy leaves the library (:func:`music_scan.release.retire`) or is deleted
    from ``quarantine/replaced/`` (``music-album-audit-releases --replaced``,
    the automatic purge) only when it does.

    It also knows the files merges moved to ``quarantine/replaced/`` onto these
    items (:data:`MERGED_FILES`): the merge decided the keeper holds their
    recording, whatever their own identity tags say (#303).  A name is not an
    ID, though, and a wrong merge leaves its loser's name behind
    (homelab#2539), so a merged file counts only while its title and artist
    agree with a keeper's (:func:`merge_agrees`).

    A Spotify ID or ISRC holds a recording outright.  A MusicBrainz recording ID
    (``mb:``) holds it only from an item whose length agrees
    (:func:`same_mb_recording`, homelab#2540): beets' recording is a best guess
    per file, and a sibling recording at another length is not a copy."""

    def __init__(self, items=()) -> None:
        self.ids: set[str] = set()
        self.mb_lengths: dict[str, list] = {}
        self.merged: dict[str, list[MergeKeeper]] = {}
        for item in items:
            for key in item_recording_ids(item):
                if key.startswith("mb:"):
                    self.mb_lengths.setdefault(key, []).append(item.get("length"))
                else:
                    self.ids.add(key)
            names = merged_files(item)
            if names:
                keeper = MergeKeeper(item.id, item.get("title") or "",
                                     item.get("artist") or item.get("albumartist") or "")
                for name in names:
                    self.merged.setdefault(name, []).append(keeper)

    def holds(self, ids: frozenset[str], length=None) -> bool:
        """True when a key of *ids* (one recording's :func:`recording_ids`) is
        held: a Spotify ID or ISRC by any item, a MusicBrainz recording ID by an
        item whose length agrees with *length* (unknown agrees)."""
        if not self.ids.isdisjoint(ids):
            return True
        return any(lengths_agree(length, held) for key in ids if key.startswith("mb:")
                   for held in self.mb_lengths.get(key, ()))

    def holds_merged(self, name: str, title: str, artist: str) -> bool:
        """True when a merge moved the replaced file *name* onto one of these
        items and the file's *title* and *artist* (its tags) agree with that
        keeper's (:func:`merge_agrees`, homelab#2539)."""
        return any(merge_agrees(title, artist, k.title, k.artist) for k in self.merged.get(name, ()))

    def merge_keepers(self, name: str) -> list[MergeKeeper]:
        """The items whose :data:`MERGED_FILES` name the replaced file *name*."""
        return self.merged.get(name, [])

    def holds_item(self, item) -> bool:
        return self.holds(item_recording_ids(item), item.get("length"))

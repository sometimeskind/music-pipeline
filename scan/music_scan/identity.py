"""Track identity: Spotify track IDs and ISRCs carried on beets items (#176).

A Spotify track ID belongs to a playlist entry, not to a file: one recording has
different track IDs on the single, the album and compilations.  So an item
holds several in the ``spotify_ids`` flex attr, a comma list like ``sources``.
``isrc`` is beets' own field: every ISRC of the recording joined by ``;``, the
union of MusicBrainz's and Spotify's (they can disagree).

Identity ladder, first hit wins: Spotify track ID, ISRC, MusicBrainz recording
ID, then title+artist words as a last resort that callers log.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import NamedTuple

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
    """One entry of an album record's per-playlist track list in .albums.json.

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

    def __init__(self, items) -> None:
        self.items = list(items)
        self.by_id: dict[str, object] = {}
        self.by_isrc: dict[str, object] = {}
        self.by_words: dict[frozenset, object] = {}
        for item in self.items:
            for sid in item_spotify_ids(item):
                self.by_id.setdefault(sid, item)
            for code in item_isrcs(item):
                self.by_isrc.setdefault(code, item)
            if words := item_words(item):
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
        """Match a .spotdl song entry."""
        return self.match(
            song.get("song_id") or spotify_id(song.get("url")), song.get("isrc"),
            song.get("name", ""), (song.get("artists") or [""])[0], words,
        )

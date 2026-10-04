"""Track identity: Spotify track IDs and ISRCs carried on beets items (#176).

A Spotify track ID belongs to a playlist entry, not to a file: one recording has
different track IDs on the single, the album and compilations.  So an item
holds several in the ``spotify_ids`` flex attr, a comma list like ``sources``.
``isrc`` is beets' own field; MusicBrainz fills it with every ISRC of the
recording joined by ``;``.
"""

from __future__ import annotations

SPOTIFY_TRACK_URL = "https://open.spotify.com/track/"


def spotify_id(url: str | None) -> str | None:
    """The track ID in a Spotify track URL (query string dropped), or None."""
    if not url or not url.startswith(SPOTIFY_TRACK_URL):
        return None
    return url[len(SPOTIFY_TRACK_URL):].split("?")[0].strip("/") or None


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

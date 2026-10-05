"""Thin wrapper around the beets Library API for the operations we need."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from music_scan.identity import SPOTIFY_TRACK_URL, ItemIndex, split_list

if TYPE_CHECKING:
    from beets.library import Item

logger = logging.getLogger(__name__)

LIBRARY_DB = Path("/root/.config/beets/library.db")
LIBRARY_DIR = Path("/root/Music/library")
# mediafile's type names (Item.format) for lossless codecs. Matched by codec,
# not extension, so ALAC inside .m4a counts too.
LOSSLESS_FORMATS = frozenset({"FLAC", "ALAC", "APE", "WavPack", "AIFF", "WAVE", "DSD Stream File"})


class MusicLibrary:
    """Context-manager wrapper around beets.library.Library."""

    def __init__(self, db_path: Path = LIBRARY_DB, directory: Path = LIBRARY_DIR) -> None:
        from beets.library import Library  # deferred — not available in tests without beets

        # Pass directory explicitly: beets >=2.10.0 stores paths relative to the
        # library root and needs this to reconstruct absolute paths correctly.
        self._lib = Library(str(db_path), directory=str(directory))

    def __enter__(self) -> "MusicLibrary":
        return self

    def __exit__(self, *_: object) -> None:
        self._lib._close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def items_by_source(self, source: str) -> list["Item"]:
        """All items whose source flexible-attribute matches *source*."""
        return list(self._lib.items(f"sources:{source}"))

    def all_items(self) -> list["Item"]:
        return list(self._lib.items())

    def get_item(self, item_id: int) -> "Item | None":
        return self._lib.get_item(item_id)

    def item_count(self) -> int:
        """Return the total number of items in the library."""
        return sum(1 for _ in self._lib.items())

    def lossless_item_count(self) -> int:
        """Items with a lossless codec. The library should hold none: the convert
        plugin transcodes them on import, but imports the original without an
        error when ffmpeg fails."""
        return sum(1 for item in self._lib.items() if item.format in LOSSLESS_FORMATS)

    def items_added_since(self, since: float) -> list[tuple[str, str]]:
        """Return (title, artist) for items added to the library after *since* (Unix timestamp)."""
        return [
            (item.title or "", item.artist or item.albumartist or "")
            for item in self._lib.items()
            if (item.added or 0) >= since
        ]

    def paths_by_source(self, source: str) -> list[Path]:
        """File paths for all items with the given source tag."""
        items = self.items_by_source(source)
        return [
            Path(item.path.decode() if isinstance(item.path, bytes) else item.path)
            for item in items
        ]

    def spotify_urls_by_source(self, source: str) -> frozenset[str]:
        """Spotify track URLs of all items with the given source tag.

        Built from ``spotify_ids`` too: an item that took a second playlist as
        a duplicate keeps the first playlist's ``spotify_url``.
        """
        urls: set[str] = set()
        for item in self.items_by_source(source):
            if item.get("spotify_url"):
                urls.add(item.get("spotify_url"))
            urls.update(SPOTIFY_TRACK_URL + sid for sid in split_list(item.get("spotify_ids")))
        return frozenset(urls)

    # ------------------------------------------------------------------
    # Modification helpers
    # ------------------------------------------------------------------

    def clear_source_tag(
        self, title: str, artist: str, source: str, spotify_id: str | None = None, isrc: str | None = None
    ) -> bool:
        """Clear the source tag on the item the removed playlist entry maps to.

        Matches the entry's Spotify ID, then its ISRC, among the *source* items.
        Without either hit (or entries queued before #176), falls back to
        title + artist with beets' substring query — beets has no contains-word
        query; clash validation in load_playlists() prevents false positives —
        and logs it.  Returns True if at least one item was modified.
        """
        item, _ = ItemIndex(self.items_by_source(source)).match(spotify_id, isrc, words=False)
        if item is not None:
            items = [item]
        else:
            # Substring match on sources field; load_playlists() ensures no name clashes.
            query = f"title:{title} artist:{artist} sources:{source}"
            items = list(self._lib.items(query))
            if not items:
                return False
            logger.info("  [WORDS] %s: removed %s — %s matched by title+artist only", source, title, artist)
        for item in items:
            parts = [p.strip() for p in (item.get("sources") or "").split(",")]
            item["sources"] = ",".join(p for p in parts if p and p != source)
            item.store()
        logger.debug("Cleared source=%s on %d item(s) for %s — %s", source, len(items), title, artist)
        return True

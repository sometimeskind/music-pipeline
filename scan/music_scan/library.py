"""Thin wrapper around the beets Library API for the operations we need."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beets.library import Item

logger = logging.getLogger(__name__)

LIBRARY_DB = Path("/root/.config/beets/library.db")
LIBRARY_DIR = Path("/root/Music/library")


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

    def item_count(self) -> int:
        """Return the total number of items in the library."""
        return sum(1 for _ in self._lib.items())

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
        """Spotify URLs stored as flex attr for all items with the given source tag."""
        return frozenset(
            item.get("spotify_url")
            for item in self.items_by_source(source)
            if item.get("spotify_url")
        )

    # ------------------------------------------------------------------
    # Modification helpers
    # ------------------------------------------------------------------

    def clear_source_tag(self, title: str, artist: str, source: str) -> bool:
        """Clear the source tag on items matching title + artist + source.

        Returns True if at least one item was modified.
        Matching is done with beets' substring query — beets has no contains-word
        query; clash validation in load_playlists() prevents false positives.
        """
        # Substring match on sources field; load_playlists() ensures no name clashes.
        query = f"title:{title} artist:{artist} sources:{source}"
        items = list(self._lib.items(query))
        if not items:
            return False
        for item in items:
            parts = [p.strip() for p in (item.get("sources") or "").split(",")]
            item["sources"] = ",".join(p for p in parts if p and p != source)
            item.store()
        logger.debug("Cleared source tag on %d item(s) matching %r", len(items), query)
        return True

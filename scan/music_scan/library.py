"""Thin wrapper around the beets Library API for the operations we need."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beets.library import Item

logger = logging.getLogger(__name__)

LIBRARY_DB = Path("/root/.config/beets/library.db")


class MusicLibrary:
    """Context-manager wrapper around beets.library.Library."""

    def __init__(self, db_path: Path = LIBRARY_DB) -> None:
        from beets.library import Library  # deferred — not available in tests without beets

        self._lib = Library(str(db_path))

    def __enter__(self) -> "MusicLibrary":
        return self

    def __exit__(self, *_: object) -> None:
        self._lib._close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def items_by_source(self, source: str) -> list["Item"]:
        """All items whose source flexible-attribute matches *source*."""
        return list(self._lib.items(f"source:{source}"))

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

    # ------------------------------------------------------------------
    # Modification helpers
    # ------------------------------------------------------------------

    def clear_source_tag(self, title: str, artist: str, source: str) -> bool:
        """Clear the source tag on items matching title + artist + source.

        Returns True if at least one item was modified.
        Matching is done with beets' query syntax (substring by default).
        """
        query = f"title:{title} artist:{artist} source:{source}"
        items = list(self._lib.items(query))
        if not items:
            return False
        for item in items:
            item["source"] = ""
            item.store()
        logger.debug("Cleared source tag on %d item(s) matching %r", len(items), query)
        return True

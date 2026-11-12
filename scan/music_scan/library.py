"""Thin wrapper around the beets Library API for the operations we need."""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from beets.library import Item

logger = logging.getLogger(__name__)

LIBRARY_DB = Path("/root/.config/beets/library.db")
LIBRARY_DIR = Path("/root/Music/library")
# mediafile's type names (Item.format) for lossless codecs. Matched by codec,
# not extension, so ALAC inside .m4a counts too.
LOSSLESS_FORMATS = frozenset({"FLAC", "ALAC", "APE", "WavPack", "AIFF", "WAVE", "DSD Stream File"})


@functools.cache
def _library_class() -> type:
    """beets' ``Library`` with a transaction that lets go when its COMMIT fails (#506).

    beets' ``Transaction.__exit__`` commits and only then releases the
    database's ``_db_lock``, not in a ``finally``.  A COMMIT that can't get
    SQLite's exclusive lock (a reader on another connection past the busy
    timeout, 5 s) raises ``database is locked`` and skips the release, so:

    - the SQLite transaction stays open, holding its PENDING lock, and every
      other connection's reads and writes fail with ``database is locked``;
    - ``_db_lock`` (a plain ``threading.Lock``) stays held, so the next
      transaction on the same library blocks forever, and nothing ever ends
      the open one.

    That hung album import ``tomato-gaur`` for 4 h on 2026-11-12, and with it
    every scan, album tick and playlist sync.  Here a failed COMMIT rolls the
    transaction back and releases the lock, and the error still raises: the
    write is lost as when its UPDATE fails (canon's ``[RETAG] … failed,
    retried next run``), and the library stays usable."""
    import sqlite3  # noqa: PLC0415

    from beets.dbcore.db import Transaction  # noqa: PLC0415
    from beets.library import Library  # noqa: PLC0415

    class _Transaction(Transaction):
        def __exit__(self, exc_type, exc_value, traceback):
            with self.db._tx_stack() as stack:
                root = len(stack) == 1 and stack[0] is self
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            except sqlite3.Error as exc:
                # What beets raises itself after the release isn't a sqlite3
                # error, so this is the root's COMMIT.
                if not root:
                    raise
                try:
                    self.db._connection().rollback()
                except sqlite3.Error:
                    logger.exception("[DBLOCK] Rolling back after a failed commit failed too")
                finally:
                    self.db._db_lock.release()
                logger.warning("[DBLOCK] A beets library commit failed (%s); its transaction was rolled back", exc)
                raise

    class _Library(Library):
        def transaction(self) -> Transaction:
            return _Transaction(self)

    return _Library


class MusicLibrary:
    """Context-manager wrapper around beets.library.Library."""

    def __init__(self, db_path: Path = LIBRARY_DB, directory: Path = LIBRARY_DIR) -> None:
        # beets is imported there, not here: it is not available in tests without
        # it.  Pass directory explicitly: beets >=2.10.0 stores paths relative to
        # the library root and needs this to reconstruct absolute paths correctly.
        self._lib = _library_class()(str(db_path), directory=str(directory))

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

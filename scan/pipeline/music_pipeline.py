"""Beets plugin: tag-on-import and conditional duplicate replacement.

Responsibilities
----------------
1. **tag_source** (``import_task_choice``): every track imported from the
   spotdl inbox gets two flexible attributes:

   * ``source=<playlist>``  — playlist membership; drives .m3u generation,
     ``clear_source_tag``, and ``music-remove``.  All existing pipeline code
     reads this unchanged.
   * ``via=spotdl``          — import origin; used *only* to decide whether a
     duplicate can be replaced safely.  Never inherited.

   Handles both singleton (``task.item``) and album (``task.items``) import
   tasks.

2. **Duplicate protection** (also ``import_task_choice``): when duplicates
   exist in the library for the incoming track:

   * Any duplicate has no ``via=spotdl`` (manually imported) → skip the
     incoming file AND delete it from the inbox so it does not get
     re-attempted on every subsequent beet-import run.
   * All duplicates have ``via=spotdl`` → do nothing; ``duplicate_action:
     remove`` in config.yaml handles removal automatically.

Note
----
beets 2.7.1 does not expose an ``import_task_duplicate_action`` event.
Duplicate handling must be done inside ``import_task_choice``, which fires
just before ``_resolve_duplicates()`` is called by the importer pipeline.

Known limitations
-----------------
**PVC-loss recovery:** after a wiped database, no existing items exist so no
duplicate check fires. After the rebuild completes all tracks will carry
``via=spotdl`` for future runs. No manual intervention needed.
"""

from pathlib import Path

from beets import importer as beets_importer
from beets import library as beets_library
from beets.plugins import BeetsPlugin

SPOTDL_INBOX = Path("/root/Music/inbox/spotdl")

# Actions that indicate the task will actually be applied to the library.
# Matches the guard used by beets' own _resolve_duplicates().
_WILL_APPLY = (
    beets_importer.Action.APPLY,
    beets_importer.Action.ASIS,
    beets_importer.Action.RETAG,
)


def _playlist_from_path(path: str | bytes) -> str | None:
    """Return the playlist name if *path* is inside SPOTDL_INBOX, else None."""
    if isinstance(path, bytes):
        path = path.decode()
    try:
        rel = Path(path).relative_to(SPOTDL_INBOX)
        return rel.parts[0]
    except (ValueError, IndexError):
        return None


def _all_via_spotdl(duplicates: list) -> bool:
    """Return True if every duplicate in *duplicates* carries via=spotdl."""
    return all((item.get("via") or "") == "spotdl" for item in duplicates)


class MusicPipelinePlugin(BeetsPlugin):
    def __init__(self):
        super().__init__("music_pipeline")
        self.register_listener("import_task_choice", self.tag_source)
        self.register_listener("item_imported", self.tag_imported_item)

    def tag_imported_item(self, lib, item):
        """Ensure source= is set for items imported from the spotdl inbox.

        Fires after every import regardless of autotag mode (ASIS or matched).
        Complements tag_source: if import_task_choice did not fire or its
        modification was not persisted, this sets source= and stores the item.
        """
        playlist = _playlist_from_path(item.path)
        if playlist is None:
            return
        if (item.get("source") or "") == playlist:
            return  # already set correctly by tag_source
        item["source"] = playlist
        item["via"] = "spotdl"
        item.store()
        self._log.debug(
            "tagged imported item source={} via=spotdl (item_imported): {}", playlist, item.path
        )

    def tag_source(self, session, task):
        """Tag incoming tracks with source= and via=, then handle duplicates."""
        if getattr(task, "item", None) is not None:
            items = [task.item]
        else:
            items = list(getattr(task, "items", None) or [])

        for item in items:
            playlist = _playlist_from_path(item.path)
            if playlist is None:
                continue
            item["source"] = playlist
            item["via"] = "spotdl"
            self._log.debug(
                "tagged incoming track source={} via=spotdl: {}", playlist, item.path
            )

        # Only check duplicates for tasks that will be applied to the library.
        # Mirrors the guard in beets' own _resolve_duplicates().
        if not items or task.choice_flag not in _WILL_APPLY:
            return

        try:
            found = task.find_duplicates(session.lib)
        except Exception as exc:
            self._log.warning("could not check for duplicates: {}", exc)
            return

        if not found:
            return

        # Flatten album duplicates to individual items for via= inspection.
        dup_items = []
        for dup in found:
            if isinstance(dup, beets_library.Album):
                dup_items.extend(dup.items())
            else:
                dup_items.append(dup)

        if _all_via_spotdl(dup_items):
            # All spotdl-sourced — let duplicate_action: remove in config
            # handle removal automatically.
            self._log.debug(
                "spotdl-only duplicate(s) found; deferring to config duplicate_action"
            )
            return

        # At least one manually-imported copy — protect it.
        # Delete the incoming spotdl file from the inbox so it doesn't
        # linger and get re-attempted on every beet import run.
        for item in items:
            path = item.path if isinstance(item.path, str) else item.path.decode()
            try:
                Path(path).unlink(missing_ok=True)
                self._log.debug(
                    "discarded spotdl inbox file protected by"
                    " non-spotdl duplicate: {}",
                    path,
                )
            except OSError as exc:
                self._log.warning(
                    "could not remove skipped inbox file {}: {}", path, exc
                )

        task.set_choice(beets_importer.Action.SKIP)
        self._log.debug("skipping import: manual duplicate exists in library")

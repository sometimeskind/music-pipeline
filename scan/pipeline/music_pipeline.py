"""Beets plugin: tag-on-import and conditional duplicate replacement.

Responsibilities
----------------
1. **tag_source_on_created** (``import_task_created``): every track imported
   from the spotdl inbox gets two flexible attributes:

   * ``source=<playlist>``  — playlist membership; drives .m3u generation,
     ``clear_source_tag``, and ``music-remove``.  All existing pipeline code
     reads this unchanged.
   * ``via=spotdl``          — import origin; used *only* to decide whether a
     duplicate can be replaced safely.  Never inherited.

   Fires from ``handle_created()`` during ``read_tasks`` — always, regardless
   of autotag mode — while ``item.path`` still points to the inbox file.
   Handles both singleton (``task.item``) and album (``task.items``) import
   tasks.

   Caches both ``filename → playlist`` and ``title → playlist`` in
   ``_pending_sources`` for step 1a.

1a. **tag_source_on_stored** (``item_imported``): re-applies ``source=`` and
    ``via=`` after the item is fully persisted, then calls ``item.store()``.
    Two things can discard the flex attributes between steps 1 and 1a:
    (a) beets' dirty-field tracking is reset during candidate lookup, so
    flex attributes set in memory at ``import_task_created`` are not marked
    dirty for the eventual ``item.add(lib)`` call; and
    (b) beets renames the file on import (spotdl names ``Artist - Title.m4a``;
    beets renames to ``NN - Title.m4a``), so the inbox filename no longer
    matches ``item.path`` at ``item_imported`` time.  The title-based key
    survives both.  This hook guarantees the tags land in the database on
    clean first-time imports.

2. **Duplicate protection** (``import_task_choice``): when duplicates exist in
   the library for the incoming track:

   * Any duplicate has no ``via=spotdl`` (manually imported) → skip the
     incoming file AND delete it from the inbox so it does not get
     re-attempted on every subsequent beet-import run.
   * All duplicates have ``via=spotdl`` → do nothing; ``duplicate_action:
     remove`` in config.yaml handles removal automatically.

   Only fires when ``autotag=True`` (production). In ASIS mode
   (``autotag=False``), ``import_asis`` calls ``_resolve_duplicates``
   directly using ``config duplicate_action``.

Note
----
``import_task_start`` is inside ``lookup_candidates`` and only fires when
``autotag=True``; it is never emitted in ASIS mode.  ``import_task_created``
is the earliest hook that works in both modes.

beets does not expose an ``import_task_duplicate_action`` event.
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

# The two locations where spotdl downloads land at beet-import time.
# 1. Main inbox: downloaded files sit here until beet import runs.
SPOTDL_INBOX = Path("/root/Music/inbox/spotdl")
# 2. ASIS staging: files that failed autotag are quarantined, then
#    _move_asis_eligible() copies them into a per-run temp dir under /tmp/
#    preserving the spotdl/<playlist>/ subdirectory structure.
ASIS_STAGING_ROOT = Path("/tmp")

# Actions that indicate the task will actually be applied to the library.
# Matches the guard used by beets' own _resolve_duplicates().
_WILL_APPLY = (
    beets_importer.Action.APPLY,
    beets_importer.Action.ASIS,
    beets_importer.Action.RETAG,
)


def _playlist_from_path(path: str | bytes) -> str | None:
    """Return the playlist name for *path*, or None if not recognisable.

    Checks two known locations in order:
      1. Main inbox:    /root/Music/inbox/spotdl/<playlist>/<file>
      2. ASIS staging:  /tmp/asis-staging-X/spotdl/<playlist>/<file>
         (the staging dir name is random, but spotdl/ is always the first
         subdir of the playlist tree relative to ASIS_STAGING_ROOT)
    """
    if isinstance(path, bytes):
        path = path.decode()
    p = Path(path)

    # 1. Main inbox — spotdl/<playlist>/ is the root itself.
    try:
        return p.relative_to(SPOTDL_INBOX).parts[0]
    except (ValueError, IndexError):
        pass

    # 2. ASIS staging — /tmp/<staging-dir>/spotdl/<playlist>/<file>
    try:
        after_tmp = p.relative_to(ASIS_STAGING_ROOT)
        # after_tmp.parts: ('<staging-dir>', 'spotdl', '<playlist>', '<file>')
        idx = after_tmp.parts.index("spotdl")
        return after_tmp.parts[idx + 1]
    except (ValueError, IndexError):
        pass

    return None


def _all_via_spotdl(duplicates: list) -> bool:
    """Return True if every duplicate in *duplicates* carries via=spotdl."""
    return all((item.get("via") or "") == "spotdl" for item in duplicates)


def _items_from_task(task) -> list:
    """Return the list of items for a task (singleton or album)."""
    if getattr(task, "item", None) is not None:
        return [task.item]
    return list(getattr(task, "items", None) or [])


class MusicPipelinePlugin(BeetsPlugin):
    def __init__(self):
        super().__init__("music_pipeline")
        # Keys: inbox filename OR normalised title → playlist name.
        # Populated at import_task_created, consumed at item_imported.
        # Two keys per track because beets renames the file on import
        # (spotdl names "Artist - Title.m4a"; beets renames to "NN - Title.m4a"),
        # so the filename key no longer matches at item_imported time.
        # The title key survives both the rename and MB autotag metadata replacement.
        self._pending_sources: dict[str, str] = {}
        self.register_listener("import_task_created", self.tag_source_on_created)
        self.register_listener("item_imported", self.tag_source_on_stored)
        self.register_listener("import_task_choice", self.handle_duplicates)

    def tag_source_on_created(self, session, task):
        """Tag incoming tracks with source= and via= at task creation.

        Fires from handle_created() during read_tasks — always, regardless of
        autotag mode — while item.path still points to the inbox file (before
        any pipeline stage runs).

        Caches both the inbox filename and the normalised title in
        _pending_sources so tag_source_on_stored can re-apply the tags after
        MusicBrainz autotag may have discarded them and beets has renamed the
        file.
        """
        for item in _items_from_task(task):
            playlist = _playlist_from_path(item.path)
            if playlist is None:
                continue
            item["source"] = playlist
            item["via"] = "spotdl"
            path = item.path.decode() if isinstance(item.path, bytes) else item.path
            self._pending_sources[Path(path).name] = playlist
            title = (item.title or "").lower()
            if title:
                self._pending_sources[title] = playlist
            self._log.debug(
                "tagged incoming track source={} via=spotdl: {}", playlist, item.path
            )

    def tag_source_on_stored(self, lib, item):
        """Re-apply source= and via= after the item is persisted to the library.

        Fires from item_imported, after autotag and item.store() have run.
        MusicBrainz autotag can replace the item's metadata dict wholesale and
        beets renames the file, both of which discard the flex attributes set
        earlier by tag_source_on_created.  We try the post-rename filename
        first (handles no-rename edge cases), then fall back to the normalised
        title (the common case after a move-import).
        """
        path = item.path.decode() if isinstance(item.path, bytes) else item.path
        title = (item.title or "").lower()
        playlist = self._pending_sources.pop(Path(path).name, None)
        if playlist is not None:
            # Matched on filename — also clean the title key to avoid stale entries.
            if title:
                self._pending_sources.pop(title, None)
        elif title:
            playlist = self._pending_sources.pop(title, None)
            # The original spotdl inbox filename key cannot be recovered from
            # item.path (which is now the renamed library path), so it may
            # remain in _pending_sources.  That is harmless: _pending_sources
            # is session-scoped and is GC'd when the import session ends.
        if playlist is None:
            return
        item["source"] = playlist
        item["via"] = "spotdl"
        item.store()
        self._log.debug(
            "persisted source={} via=spotdl on stored item: {}", playlist, item.path
        )

    def handle_duplicates(self, session, task):
        """Protect manually-imported tracks from spotdl overwrites.

        Fires from user_query (autotag=True only). In ASIS mode
        (autotag=False) beets applies duplicate_action from config directly.
        """
        items = _items_from_task(task)

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

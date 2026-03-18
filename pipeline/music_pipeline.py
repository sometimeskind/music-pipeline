"""Beets plugin: tag-on-import and conditional duplicate replacement.

Responsibilities
----------------
1. **tag_source** (``import_task_choice``): every track imported from the
   spotdl inbox gets two flexible attributes:

   * ``source=<playlist>``  — playlist membership; drives .m3u generation,
     ``clear_source_tag``, and ``music-remove``.  All existing pipeline code
     reads this unchanged.
   * ``via=spotdl``          — import origin; used *only* to decide whether a
     duplicate can be replaced.  Never inherited.

   Handles both singleton (``task.item``) and album (``task.items``) import
   tasks.

2. **duplicate_action** (``import_task_duplicate_action``): when beets finds
   an existing library item that matches the incoming track:

   * All existing duplicates have ``via=spotdl``  → replace them; incoming
     file inherits ``source=`` so the track stays in its playlists.
   * Any duplicate has no ``via=`` (manually imported, even if it carries a
     ``source=`` tag) → skip the incoming file AND delete it from the inbox so
     it does not get re-attempted on every subsequent beet-import run.

Known limitations
-----------------
**PVC-loss recovery:** after a wiped database, no existing items exist so no
duplicate check fires. After the rebuild completes all tracks will carry
``via=spotdl`` for future runs. No manual intervention needed.
"""

from pathlib import Path

from beets import importer as beets_importer
from beets.plugins import BeetsPlugin

SPOTDL_INBOX = Path("/root/Music/inbox/spotdl")


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
        self.register_listener(
            "import_task_duplicate_action", self.duplicate_action
        )

    # ------------------------------------------------------------------
    # Listener 1: set source= and via= on every spotdl import
    # ------------------------------------------------------------------

    def tag_source(self, session, task):
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

    # ------------------------------------------------------------------
    # Listener 2: decide what to do when a duplicate is detected
    # ------------------------------------------------------------------

    def duplicate_action(self, session, task, duplicates):
        # duplicates is passed as a keyword arg by beets, not stored on task
        if not duplicates:
            return None

        item = getattr(task, "item", None)

        if not _all_via_spotdl(duplicates):
            # At least one manually-imported copy — protect it.
            # Delete the incoming spotdl file from the inbox so it doesn't
            # linger and get re-attempted on every beet import run.
            if item is not None:
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
            return beets_importer.action.SKIP

        # All duplicates are spotdl-sourced — replace them.
        # Inherit source= (playlist membership) from the existing item so the
        # track stays in its .m3u playlists.
        # Note: only the first non-empty source= is inherited. If the same
        # track somehow exists in two spotdl playlists, the second membership
        # is lost. In practice each track belongs to one playlist at a time.
        if item is not None:
            inherited = next(
                (d["source"] for d in duplicates if d.get("source")), ""
            )
            if inherited and not (item.get("source") or ""):
                item["source"] = inherited
            self._log.debug(
                "replacing spotdl duplicate (source={}) incoming via={}",
                inherited,
                item.get("via") or "",
            )
        return beets_importer.action.REMOVE

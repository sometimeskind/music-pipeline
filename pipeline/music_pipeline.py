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

2. **duplicate_action** (``import_task_choice``): before beets' own duplicate
   resolution runs, check for manually-imported duplicates:

   * Any duplicate has no ``via=spotdl`` (manually imported) → skip the
     incoming file AND delete it from the inbox so it does not get
     re-attempted on every subsequent beet-import run.
   * All existing duplicates have ``via=spotdl`` → do nothing; beets'
     ``duplicate_action: remove`` config takes over and replaces them.
     The incoming file already has ``source=`` set by ``tag_source``.

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
        self.register_listener("import_task_choice", self.duplicate_action)

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
    # Listener 2: protect manually-imported tracks from spotdl overwrites
    # ------------------------------------------------------------------

    def duplicate_action(self, session, task):
        """Check for manual-import duplicates before beets resolves them.

        Fires on import_task_choice, before beets' _resolve_duplicates stage.
        If a manually-imported (non-spotdl) duplicate exists, skip the
        incoming track and remove it from the inbox so it won't be retried.
        Spotdl-vs-spotdl replacement is delegated to beets via
        ``duplicate_action: remove`` in config.
        """
        try:
            duplicates = task.find_duplicates(session.lib)
        except Exception:
            return

        if not duplicates:
            return

        if _all_via_spotdl(duplicates):
            # All spotdl — let beets' duplicate_action: remove handle replacement.
            self._log.debug(
                "spotdl duplicate(s) found; deferring to beets duplicate_action: remove"
            )
            return

        # At least one manually-imported copy — protect it.
        # Delete the incoming spotdl file from the inbox so it doesn't
        # linger and get re-attempted on every beet import run.
        item = getattr(task, "item", None)
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
        task.set_choice(beets_importer.Action.SKIP)

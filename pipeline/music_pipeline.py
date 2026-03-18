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

2. **duplicate_action** (``import_task_duplicate_action``): when beets finds
   an existing library item that matches the incoming track:

   * All existing duplicates have ``via=spotdl``  → replace them; incoming
     file inherits ``source=`` so the track stays in its playlists.
   * Any duplicate has no ``via=`` (manually imported, even if it carries a
     ``source=`` tag) → skip the incoming file AND delete it from the inbox so
     it does not get re-attempted on every subsequent beet-import run.
"""

from pathlib import Path

from beets import importer as beets_importer
from beets.plugins import BeetsPlugin

SPOTDL_INBOX = Path("/root/Music/inbox/spotdl")


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
        item = getattr(task, "item", None)
        if item is None:
            return
        path = item.path
        if isinstance(path, bytes):
            path = path.decode()
        try:
            rel = Path(path).relative_to(SPOTDL_INBOX)
            playlist = rel.parts[0]
        except (ValueError, IndexError):
            return  # not from spotdl inbox — leave tags alone
        item["source"] = playlist
        item["via"] = "spotdl"
        self._log.debug(
            "tagged incoming track source={} via=spotdl: {}", playlist, path
        )

    # ------------------------------------------------------------------
    # Listener 2: decide what to do when a duplicate is detected
    # ------------------------------------------------------------------

    def duplicate_action(self, config, task):
        duplicates = getattr(task, "duplicates", None)
        if not duplicates:
            return None

        vias = [item.get("via") or "" for item in duplicates]

        if any(v != "spotdl" for v in vias):
            # At least one manually-imported copy — protect it.
            # Delete the incoming spotdl file from the inbox so it doesn't
            # linger and get re-attempted on every beet import run.
            item = getattr(task, "item", None)
            if item is not None:
                path = item.path
                if isinstance(path, bytes):
                    path = path.decode()
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
        # Do NOT inherit via= — tag_source already set it on the incoming item
        # if it came from the spotdl inbox.
        item = getattr(task, "item", None)
        if item is not None:
            inherited_source = next(
                (d.get("source") or "" for d in duplicates if d.get("source")),
                "",
            )
            if inherited_source and not (item.get("source") or ""):
                item["source"] = inherited_source
            self._log.debug(
                "replacing spotdl duplicate (source={}) incoming via={}",
                inherited_source,
                item.get("via") or "",
            )
        return beets_importer.action.REMOVE

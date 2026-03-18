"""Beets plugin: replace source=<playlist> duplicates on import.

If every existing duplicate of an incoming track has a non-empty source tag
(indicating it was imported via a spotdl playlist), remove those duplicates
and import the new file, inheriting the source tag so the track stays in
its .m3u playlists.

If any duplicate has no source tag (manually imported, higher-quality origin),
keep it and skip the incoming file instead.
"""

from beets.plugins import BeetsPlugin
from beets import importer as beets_importer


class SpotdlReplacePlugin(BeetsPlugin):
    def __init__(self):
        super().__init__("spotdl_replace")
        self.register_listener(
            "import_task_duplicate_action", self.duplicate_action
        )

    def duplicate_action(self, config, task):
        duplicates = getattr(task, "duplicates", None)
        if not duplicates:
            return None

        sources = [item.get("source") or "" for item in duplicates]

        if any(s == "" for s in sources):
            # At least one manually-imported copy — keep it.
            self._log.debug(
                "keeping non-spotdl duplicate for: {}", getattr(task, "item", task)
            )
            return beets_importer.action.SKIP

        # All duplicates are spotdl-sourced — inherit the first source tag
        # and replace with the incoming file.
        inherited_source = next((s for s in sources if s), "")
        item = getattr(task, "item", None)
        if item is not None and inherited_source:
            item["source"] = inherited_source
            self._log.debug(
                "replacing source={} duplicate, inheriting tag for: {}",
                inherited_source,
                item,
            )
        return beets_importer.action.REMOVE

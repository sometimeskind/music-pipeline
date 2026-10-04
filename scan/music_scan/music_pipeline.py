"""Beets plugin: tag-on-import and multi-playlist membership via duplicate append.

Responsibilities
----------------
1. **tag_source_on_created** (``import_task_created``): every track imported
   from the spotdl inbox gets two flexible attributes:

   * ``sources=<playlist>``  — playlist membership; drives .m3u generation,
     ``clear_source_tag``, and ``music-remove``.  Comma-separated when a track
     belongs to multiple playlists (appended by handle_duplicates).
   * ``spotify_ids=<id>``     — Spotify track IDs of the playlist entries the
     item satisfies (#176), a comma list appended by handle_duplicates.  Read
     with ``isrc`` from spotdl's ``----:spotdl:*`` atoms, which ``scrub``
     strips, so the DB is the only copy.  spotdl's ISRC is added to
     MusicBrainz's (``;``-joined; they can disagree).  ``spotify_url`` keeps
     the first URL.
   * ``via=spotdl|usenet``    — import origin (``usenet`` for album mode's
     ``inbox/usenet/<playlist>/``); used *only* to decide whether a
     duplicate can be replaced safely.  Never inherited.

   Fires from ``handle_created()`` during ``read_tasks`` — always, regardless
   of autotag mode — while ``item.path`` still points to the inbox file.
   Handles both singleton (``task.item``) and album (``task.items``) import
   tasks.

   Caches both ``filename → playlist`` and ``title → playlist`` in
   ``_pending_sources`` for step 1a.

1a. **tag_source_on_stored** (``item_imported``): re-applies ``sources=`` and
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

2. **Multi-playlist membership** (``import_task_choice``): duplicates are the
   library items that are the same recording by identity (Spotify track ID,
   ISRC or MusicBrainz recording ID, #176), else beets' artist+title
   duplicates.  An artist+title duplicate whose ISRCs are disjoint from the
   incoming track's (and with no shared recording ID) is a different
   recording — a live take, a remaster, a radio edit — and is hidden from
   beets' own check so the track imports alongside it (``[SPLIT]``).
   Artist+title merges log ``[WORDS]``.  When duplicates exist:

   * Any duplicate has a ``via`` other than spotdl/usenet (manually imported) → skip the
     incoming file AND delete it from the inbox so it does not get
     re-attempted on every subsequent beet-import run.
   * All duplicates have ``via=spotdl`` or ``usenet`` → append the incoming playlist name to
     the ``sources`` field of every existing duplicate (idempotent), delete the
     incoming inbox file, and set SKIP so beets does not re-import the track.
     If the incoming playlist cannot be resolved (cache miss and unrecognisable
     path), fall through to ``duplicate_action: remove`` with a warning.

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

import os
from pathlib import Path
from typing import NamedTuple

from beets import importer as beets_importer
from beets import library as beets_library
from beets.plugins import BeetsPlugin

from music_scan.identity import add_isrcs, add_to_list, item_isrcs, item_spotify_ids, split_list, spotify_id

# The two locations where spotdl downloads land at beet-import time.
# 1. Main inbox: downloaded files sit here until beet import runs.
SPOTDL_INBOX = Path("/root/Music/inbox/spotdl")
# Album mode (#168): whole albums from Usenet, moved here as
# usenet/<playlist>/<job>/ before the import.  Tagged via=usenet.  Never staged
# for the asis pass, so they need no staging path below.
USENET_INBOX = Path("/root/Music/inbox/usenet")
# Imports the pipeline made itself.  A duplicate carrying one of these can take
# another playlist's source tag; any other via is a manual import and protected.
MANAGED_VIA = frozenset({"spotdl", "usenet"})
# 2. ASIS staging: files that failed autotag are quarantined, then
#    _move_asis_eligible() copies them into a per-run temp dir under /tmp/
#    preserving the spotdl/<playlist>/ subdirectory structure.
ASIS_STAGING_ROOT = Path("/tmp")


class SpotdlTags(NamedTuple):
    url: str | None = None
    isrc: str | None = None


def _read_spotdl_tags(path: str | bytes) -> SpotdlTags:
    """Return the Spotify URL and ISRC spotdl embedded in an M4A file.

    spotdl writes both to its own freeform atoms, which beets doesn't read.
    Called while item.path still points to the inbox file — before the scrub
    plugin strips them.
    """
    try:
        from mutagen.mp4 import MP4  # noqa: PLC0415 — beets dep, always available

        if isinstance(path, bytes):
            path = path.decode()
        tags = MP4(path).tags or {}

        def first(key: str) -> str | None:
            raw = tags.get(key)
            if not raw:
                return None
            return raw[0].decode("utf-8").strip() or None

        return SpotdlTags(first("----:spotdl:WOAS"), first("----:spotdl:ISRC"))
    except Exception:
        return SpotdlTags()


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

    # 1. Main inbox — spotdl/<playlist>/ (or usenet/<playlist>/) is the root itself.
    for inbox in (SPOTDL_INBOX, USENET_INBOX):
        try:
            return p.relative_to(inbox).parts[0]
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


def _via_from_path(path: str | bytes) -> str:
    """``usenet`` for album-mode imports, ``spotdl`` for everything else we tag."""
    if isinstance(path, bytes):
        path = path.decode()
    return "usenet" if Path(path).is_relative_to(USENET_INBOX) else "spotdl"


def _all_via_spotdl(duplicates: list) -> bool:
    """Return True if every duplicate in *duplicates* is a pipeline import (spotdl or usenet)."""
    return all((item.get("via") or "") in MANAGED_VIA for item in duplicates)


class Incoming(NamedTuple):
    """Identity of the track being imported."""

    spotify_id: str | None
    isrcs: frozenset[str]
    mb_trackid: str | None


def _find_same_recording(lib, incoming: Incoming) -> list:
    """Library items that share a Spotify track ID, ISRC or MusicBrainz recording ID."""
    found: dict = {}
    queries = []
    if incoming.spotify_id:
        sid = incoming.spotify_id
        queries += [(f"spotify_ids:{sid}", lambda i: sid in item_spotify_ids(i)),
                    (f"spotify_url:{sid}", lambda i: sid in item_spotify_ids(i))]
    for code in incoming.isrcs:
        queries.append((f"isrc:{code}", lambda i, c=code: c in item_isrcs(i)))
    if incoming.mb_trackid:
        queries.append((f"mb_trackid:{incoming.mb_trackid}", lambda i: i.get("mb_trackid") == incoming.mb_trackid))
    for query, same in queries:
        for item in lib.items(query):
            if same(item):
                found.setdefault(item.id, item)
    return list(found.values())


def _different_recording(incoming: Incoming, dup) -> bool:
    """True when both sides have ISRCs, none shared, and no shared recording ID."""
    if incoming.mb_trackid and dup.get("mb_trackid") == incoming.mb_trackid:
        return False
    dup_isrcs = item_isrcs(dup)
    return bool(incoming.isrcs and dup_isrcs and not incoming.isrcs & dup_isrcs)


def _items_from_task(task) -> list:
    """Return the list of items for a task (singleton or album)."""
    if getattr(task, "item", None) is not None:
        return [task.item]
    return list(getattr(task, "items", None) or [])


class MusicPipelinePlugin(BeetsPlugin):
    def __init__(self):
        super().__init__("music_pipeline")
        # Patch the chroma plugin's hardcoded API key with ours.
        # beetsplug.chroma uses a module-level API_KEY constant for AcoustID
        # lookups — it ignores config["acoustid"]["apikey"] during import.
        # Overriding it here makes every beet invocation use our dedicated key
        # instead of the shared beets application key (which gets rate-limited).
        key = os.environ.get("ACOUSTID_APIKEY")
        if key:
            try:
                import beetsplug.chroma as _chroma  # noqa: PLC0415
                _chroma.API_KEY = key
            except Exception:
                pass
        # Keys: inbox filename OR normalised title → playlist name.
        # Populated at import_task_created, consumed at item_imported.
        # Two keys per track because beets renames the file on import
        # (spotdl names "Artist - Title.m4a"; beets renames to "NN - Title.m4a"),
        # so the filename key no longer matches at item_imported time.
        # The title key survives both the rename and MB autotag metadata replacement.
        self._pending_sources: dict[str, str] = {}
        self._pending_spotdl: dict[str, SpotdlTags] = {}
        # Same keys → via (spotdl or usenet).
        self._pending_via: dict[str, str] = {}
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
            via = _via_from_path(item.path)
            item["sources"] = playlist
            item["via"] = via
            path = item.path.decode() if isinstance(item.path, bytes) else item.path
            filename = Path(path).name
            self._pending_sources[filename] = playlist
            self._pending_via[filename] = via
            title = (item.title or "").lower()
            if title:
                self._pending_sources[title] = playlist
                self._pending_via[title] = via
            tags = _read_spotdl_tags(item.path)
            if tags.url or tags.isrc:
                self._apply_spotdl_tags(item, tags)
                self._pending_spotdl[filename] = tags
                if title:
                    self._pending_spotdl[title] = tags
            self._log.debug(
                "tagged incoming track source={} via={}: {}", playlist, via, item.path
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
        via = self._pending_via.pop(Path(path).name, None)
        if title:
            via = self._pending_via.pop(title, None) or via
        item["sources"] = playlist
        item["via"] = via or "spotdl"
        filename = Path(path).name
        tags = self._pending_spotdl.pop(filename, None)
        if tags is not None:
            if title:
                self._pending_spotdl.pop(title, None)
        elif title:
            tags = self._pending_spotdl.pop(title, None)
        if tags is not None:
            self._apply_spotdl_tags(item, tags)
        item.store()
        self._log.debug(
            "persisted source={} via={} on stored item: {}", playlist, item["via"], item.path
        )

    @staticmethod
    def _apply_spotdl_tags(item, tags: SpotdlTags) -> None:
        """Set spotify_url and spotify_ids, and add spotdl's ISRC to the item's."""
        if tags.url:
            item["spotify_url"] = tags.url
            add_to_list(item, "spotify_ids", spotify_id(tags.url))
        add_isrcs(item, split_list(tags.isrc, ";"))

    def _incoming_identity(self, task, item) -> Incoming:
        """Spotify ID and ISRC from spotdl, plus ISRCs and recording ID from the
        chosen MusicBrainz match (or the file's own tags for an as-is import)."""
        tags = self._incoming_spotdl_tags(item)
        try:
            info = task.chosen_info()
        except Exception:
            info = {}
        isrcs = {*split_list(tags.isrc, ";"), *split_list(info.get("isrc"), ";"), *item_isrcs(item)}
        mb_trackid = info.get("track_id") or info.get("mb_trackid") or item.get("mb_trackid") or None
        return Incoming(spotify_id(tags.url), frozenset(isrcs), mb_trackid)

    def _incoming_spotdl_tags(self, item) -> SpotdlTags:
        """spotdl tags of an inbox item: cached at task creation, else read again."""
        path = item.path if isinstance(item.path, str) else item.path.decode()
        title = (item.title or "").lower()
        return (
            self._pending_spotdl.get(Path(path).name)
            or (self._pending_spotdl.get(title) if title else None)
            or _read_spotdl_tags(path)
        )

    def handle_duplicates(self, session, task):
        """Handle duplicate tracks to support multi-playlist membership.

        Fires from user_query (autotag=True only). In ASIS mode
        (autotag=False) beets applies duplicate_action from config directly.

        When all existing duplicates are pipeline imports (spotdl or usenet), appends the incoming
        playlist name to their ``sources`` field (idempotent), deletes the
        incoming inbox file, and sets SKIP.  If the incoming playlist cannot be
        resolved (cache miss and unrecognisable path), falls through to
        ``duplicate_action: remove`` with a warning.

        When any existing duplicate is manually imported, protects it by
        deleting the incoming inbox file and setting SKIP.
        """
        items = _items_from_task(task)

        # Only check duplicates for tasks that will be applied to the library.
        # Mirrors the guard in beets' own _resolve_duplicates().
        if not items or task.choice_flag not in _WILL_APPLY:
            return

        # Album tasks (not used: singletons: yes) keep beets' artist+title check.
        incoming = self._incoming_identity(task, items[0]) if len(items) == 1 else None
        try:
            same = _find_same_recording(session.lib, incoming) if incoming else []
            found = task.find_duplicates(session.lib)
        except Exception as exc:
            self._log.warning("could not check for duplicates: {}", exc)
            return

        # Flatten album duplicates to individual items for via= inspection.
        dup_items = []
        for dup in found:
            if isinstance(dup, beets_library.Album):
                dup_items.extend(dup.items())
            else:
                dup_items.append(dup)

        if same:
            dup_items = same
        elif dup_items and incoming is not None:
            different = [d for d in dup_items if _different_recording(incoming, d)]
            dup_items = [d for d in dup_items if d not in different]
            for dup in different:
                self._log.warning(
                    "[SPLIT] {} — {}: a different recording from {} (ISRCs {} vs {}); importing it separately",
                    items[0].artist, items[0].title, dup, ",".join(sorted(incoming.isrcs)), dup.get("isrc"),
                )
            if different and not dup_items:
                # Hide them from beets' own check, or duplicate_action: remove deletes them.
                task.find_duplicates = task.duplicate_items = lambda lib: []
                return
            for dup in dup_items:
                self._log.warning(
                    "[WORDS] {} — {}: merged into {} by artist+title only", items[0].artist, items[0].title, dup
                )

        if not dup_items:
            return

        if _all_via_spotdl(dup_items):
            # Resolve the incoming playlist name.
            # Try _pending_sources first (populated by tag_source_on_created).
            # Fall back to reading the path directly if the cache missed.
            incoming_playlist = None
            for item in items:
                path = item.path if isinstance(item.path, str) else item.path.decode()
                title = (item.title or "").lower()
                incoming_playlist = (
                    self._pending_sources.get(Path(path).name)
                    or (self._pending_sources.get(title) if title else None)
                    or _playlist_from_path(item.path)
                )
                if incoming_playlist:
                    break

            if not incoming_playlist:
                self._log.warning(
                    "handle_duplicates: could not resolve incoming playlist — "
                    "falling through to duplicate_action"
                )
                return  # falls through to duplicate_action: remove

            # Append the new playlist, and the Spotify ID of the entry the
            # duplicate now also satisfies, to each existing duplicate item.
            for dup in dup_items:
                changed = add_to_list(dup, "sources", incoming_playlist)
                if incoming is not None:
                    changed = add_to_list(dup, "spotify_ids", incoming.spotify_id) or changed
                if same:
                    # Same recording by identity: keep both sides' ISRCs.
                    changed = add_isrcs(dup, sorted(incoming.isrcs)) or changed
                if changed:
                    dup.store()
                    self._log.debug(
                        "appended {} to sources on existing item: {}", incoming_playlist, dup
                    )

            # Remove the incoming inbox file and skip the import.
            for item in items:
                path = item.path if isinstance(item.path, str) else item.path.decode()
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    self._log.warning("could not remove duplicate inbox file {}: {}", path, exc)
                    # Still set SKIP below — self-heals on next scan (append is idempotent).

            task.set_choice(beets_importer.Action.SKIP)
            self._log.debug(
                "skipping duplicate import; appended {} to sources on {} existing item(s)",
                incoming_playlist,
                len(dup_items),
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

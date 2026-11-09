"""Beets plugin: tag-on-import and multi-playlist provenance via duplicate append.

Responsibilities
----------------
1. **tag_source_on_created** (``import_task_created``): every track imported
   from the spotdl inbox gets two flexible attributes:

   * ``sources=<playlist>``  — the playlists a track came from (provenance).
     Playlist membership is the playlist store's (homelab#1914), never this
     field.  Comma-separated when a track came from several playlists
     (appended by handle_duplicates).
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

   Keeps what it read in ``_pending``, by item, for step 1a.

1a. **tag_source_on_stored** (``item_imported``): re-applies ``sources=`` and
    ``via=`` after the item is fully persisted, then calls ``item.store()``.
    Two things can discard the flex attributes between steps 1 and 1a:
    (a) beets' dirty-field tracking is reset during candidate lookup, so
    flex attributes set in memory at ``import_task_created`` are not marked
    dirty for the eventual ``item.add(lib)`` call; and
    (b) beets renames the file on import (spotdl names ``Artist - Title.m4a``;
    beets renames to ``NN - Title.m4a``) and a match can change the title, and
    two tracks can share a title, so neither names the item at ``item_imported``
    time (homelab#2707).  The item object does: beets keeps it from
    ``import_task_created`` to here.  This hook guarantees the tags land in the
    database on clean first-time imports.  It also records the events that need the
    stored item's ID (homelab#2590): ``imported`` for a spotdl item (a Usenet
    item's import is its album's ``[DONE]``), ``retitled`` for a title
    step 1b put back, and ``import_alongside`` or ``kept_apart`` for each
    duplicate step 2 imported it beside.

2. **Multi-playlist membership** (``import_task_choice``): duplicates are the
   library items that are the same recording by identity (Spotify track ID,
   ISRC or MusicBrainz recording ID, #176).  A single track's artist+title
   duplicate that shares none of those is a different recording — a live
   take, a remaster, a radio edit — and is hidden from beets' own check so
   the track imports alongside it: ``[SPLIT]`` when the ISRCs are disjoint,
   ``[APART]`` when one side has none (#255).  Title words never merge two
   recordings; real ID-less duplicates are left to ``music-audit-dupes``,
   which compares fingerprints.  When duplicates exist:

   * Any duplicate has a ``via`` other than spotdl/usenet (manually imported) → skip the
     incoming file AND delete it from the inbox so it does not get
     re-attempted on every subsequent beet-import run.
   * A Usenet album track whose duplicates are all ``via=spotdl`` → import it
     anyway (``[ALBUM]``): the album copy supersedes them after the import
     (:mod:`music_scan.supersede`, #266).
   * A Usenet album track that is the same recording as another release's
     item → import it alongside (``[BOTH]``, #278, #279).  A release is one
     album, imported whole: each album keeps its own copy, and two files of
     one release that claim one recording are a wrong MusicBrainz match
     (*Love* tagged *Bodies*), never one track.  An earlier copy of the same
     album isn't merged into either: the album completion keeps one copy
     and retires the other (:mod:`music_scan.release`).
   * All duplicates have ``via=spotdl`` or ``usenet`` → append the incoming playlist name to
     the ``sources`` field of every existing duplicate (idempotent), delete the
     incoming inbox file, and set SKIP so beets does not re-import the track.
     If the incoming playlist cannot be resolved (cache miss and unrecognisable
     path), fall through to ``duplicate_action: remove`` with a warning.

   Only fires when ``autotag=True`` (production). In ASIS mode
   (``autotag=False``), ``import_asis`` calls ``_resolve_duplicates``
   directly using ``config duplicate_action``: the scan's asis pass sets it
   to ``skip``, since the main config's ``remove`` would delete the existing
   item and its file (#202).

1b. **restore_file_title** (``import_task_apply``, homelab#2544): a Usenet
    album track keeps its file's own title.  beets matches each file to the
    closest MusicBrainz recording by title, artist and length, and that can be
    a sibling (the 5.1 mix of a stereo track, an outtake, a live take) or,
    with a venue suffix and a similar length, another song.  The release's
    files name their tracks right, so the title captured at
    ``import_task_created`` goes back on and ``trackdisambig`` goes.  When the
    titles are two songs (:func:`~music_scan.identity.different_song`), the
    recording is wrong too: its IDs, ISRCs and work credits go back to the
    file's own (usually none), ``[TITLE] … (recording dropped)``.  So it is
    when only one side names a version (homelab#2609,
    :func:`~music_scan.identity.version_clash`: live, 5.1, demo, mono…, the
    file's album name covering a bare title, the recording's disambiguation
    counting as its title): *Reason To Believe (Nebraska Live)* matched to the
    studio recording, or a bare *My Father's House* to one disambiguated
    "live, 2025-04-22: …", ``[TITLE] … (recording dropped: another version,
    live)``.  A sibling's ID used to stay for the fingerprint recheck
    (homelab#2540), but the duplicate check, the release pass and the purge
    trust it first: the live take imported ``[BOTH]`` as the studio track,
    and the release pass retired it as held.  A sibling with no version word
    either side still keeps its ID, for the recheck.
    The duplicate check, which runs before, leaves out a match refused either
    way.  A match is refused too when its artist shares nothing with the
    file's (homelab#2649, :func:`~music_scan.identity.different_artist`):
    Springsteen's *I'm on Fire* matched to 5000 Volts' song of that title,
    ``[TITLE] … (recording dropped: another artist, 5000 Volts)``, and the
    file's artist goes back on too.  Any band credit, featured artist or
    spelling that shares a word passes, and a file with no artist tag, a
    compilation's, or a name words can't read keeps the match.  A generic
    file title (empty, digits, ``Track NN``) keeps beets' title and its match.
    spotdl imports keep beets' match: their titles are YouTube's, and a
    request fallback's recording AcoustID verified.  Each ``[TITLE]`` change
    is a ``retitled`` event, written at ``item_imported``.

3. **fingerprint_if_missing** (``item_imported``): ``chroma`` fingerprints at
   ``import_task_start``, which ASIS mode never sends (see the note), so asis
   imports would carry no ``acoustid_fingerprint`` for the duplicate audit
   (#210).  An item stored without one is fingerprinted from its library file
   (locally, no AcoustID lookup), and the tag is written to the file before
   the store (homelab#2723).  Setting a tag field resets the item's stored
   mtime to 0, so the next scan's ``beet update`` re-reads every file whose
   item changed in the database alone and takes the file's tags back: a
   database-only fingerprint went back to the file's empty tag.

4. **keeps_suffixed_name** (patched into ``Item.move_file`` at plugin load):
   an item whose destination another file holds sits at the name beets'
   ``unique_path`` gave it (``01 - Title.1.m4a``).  beets doesn't count that
   name as already unique, so every move of the item, such as ``beet update``
   after a tag changed, took the next free one, and the name flipped between
   ``.1`` and ``.2`` (homelab#2723).  A move onto a taken destination now
   leaves an item already at one of its suffixed names where it is.

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
import re
from pathlib import Path
from typing import NamedTuple

from beets import importer as beets_importer
from beets import library as beets_library
from beets import util as beets_util
from beets.plugins import BeetsPlugin

from music_fetch import events
from music_fetch.album_requests import SOURCE as REQUEST_SOURCE
from music_fetch.album_requests import url_recording
from music_fetch.usenet import words

from music_scan.identity import (
    Credit, add_isrcs, add_to_list, artist_credit, compilation, different_artist, different_song, fingerprint,
    item_isrcs, item_spotify_ids, same_mb_recording, split_list, spotify_id, version_clash,
)

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


# The MusicBrainz recording an album request's fallback download was verified as (homelab#2478).
FALLBACK_RECORDING = "fallback_recording"


# What a MusicBrainz match for another song wrongly says about a file: the recording's IDs
# and ISRCs, and its work and the credits beets reads from it (homelab#2544).  The artist
# stays unless it is another one (ARTIST_FIELDS): beets weighs it in the match, and a wrong
# song by the right artist is the case seen.
RECORDING_FIELDS = (
    "mb_trackid", "mb_releasetrackid", "isrc", "mb_workid", "work", "work_disambig",
    "composers", "composers_ids", "composer_sort", "lyricists", "lyricists_ids",
    "arrangers", "arrangers_ids", "remixers", "remixers_ids",
)
# What a singleton match writes about its artist (beets' TrackInfo; never the album artist):
# they go back to the file's when the artist is another one (homelab#2649).
ARTIST_FIELDS = ("artist", "artists", "artist_sort", "artists_sort", "artist_credit", "artists_credit",
                 "mb_artistid", "mb_artistids")
# A file title that names nothing: beets' is better (homelab#2544).
_GENERIC_TITLE = re.compile(r"(?:track\s*)?\d*", re.IGNORECASE)


class FileTags(NamedTuple):
    """A Usenet item's own tags, captured before beets' match overwrites them
    (homelab#2544).  Holds the item itself, so its ``id()`` can't be reused
    while the entry waits for ``import_task_apply``."""

    item: object
    title: str          # as the file has it
    own: str | None     # what goes back on: :func:`own_title`
    fields: dict        # RECORDING_FIELDS as the file has them
    artist_fields: dict  # ARTIST_FIELDS as the file has them
    # The file's artist and album artist, and whether it says it is on a compilation (homelab#2649).
    credit: Credit
    compilation: bool
    # The file's album name and its Usenet job's: a live album's covers a bare title (homelab#2609).
    album: str = ""

    def another_artist(self, matched) -> bool:
        """True when *matched* (the item after beets' match, or the match's info)
        credits an artist the file's own tags share nothing with (homelab#2649).
        A file with no artist tag, or on a compilation, says nothing against it."""
        return (bool(self.artist_fields["artist"]) and not self.compilation
                and different_artist(self.credit, artist_credit(matched)))


class Retitle(NamedTuple):
    """A Usenet item's title or trackdisambig put back at ``import_task_apply``,
    recorded as a ``retitled`` event once ``item_imported`` gives it an ID
    (homelab#2590).  Holds the item, like :class:`FileTags`."""

    item: object
    message: str
    data: dict


class Beside(NamedTuple):
    """The duplicate check's decision to import an item beside a library item
    (``[BOTH]``, ``[ALBUM]``, ``[SPLIT]``, ``[APART]``), recorded as an
    ``import_alongside`` or ``kept_apart`` event once ``item_imported`` gives
    the item an ID (homelab#2590).  Holds the item, like :class:`FileTags`."""

    item: object
    kind: str
    message: str
    data: dict


ANOTHER_SONG, ANOTHER_VERSION, ANOTHER_ARTIST = "another song", "another version", "another artist"


def wrong_recording(title: str, album: str, matched: str, disambiguation: str,
                    other_artist: bool = False) -> tuple[str | None, list[str]]:
    """Why beets' match for a Usenet file can't be the file's recording, as
    ``(why, versions)``: :data:`ANOTHER_SONG` when the file's own *title* and
    the match's are two songs (:func:`~music_scan.identity.different_song`,
    homelab#2544), :data:`ANOTHER_VERSION` with the version words when only one
    of them names a version (:func:`~music_scan.identity.version_clash`:
    the file's title and *album* (its album name, and its release's job name
    too, so a live album titled bare doesn't refuse its live recordings), the
    recording's title *matched* and its *disambiguation*; homelab#2609),
    :data:`ANOTHER_ARTIST` when neither does but *other_artist* says the match
    is another artist's (:meth:`FileTags.another_artist`, homelab#2649), else
    ``(None, [])``.

    A version word refutes the match: *Reason To Believe (Nebraska Live)*
    matched to the studio *Reason to Believe* took the studio recording's ID,
    and the release pass then retired the live take as a copy the library
    held.  The file imports as its own track with its own
    IDs instead, usually none, and the fingerprint recheck names its
    recording later."""
    if different_song(title, matched):
        return ANOTHER_SONG, []
    versions = version_clash(title, matched, a_album=album, b_notes=disambiguation)
    if versions:
        return ANOTHER_VERSION, versions
    return (ANOTHER_ARTIST, []) if other_artist else (None, [])


def _dropped_note(why: str | None, versions: list[str], artist: str = "") -> str:
    """The ``[TITLE]`` line's note on a dropped recording; *artist* is the match's."""
    if why == ANOTHER_VERSION:
        return f" (recording dropped: another version, {', '.join(versions)})"
    if why == ANOTHER_ARTIST:
        return f" (recording dropped: another artist, {artist})"
    return " (recording dropped)" if why else ""


def own_title(title: str | None, track) -> str | None:
    """The title a release file gives its track, or None when it names nothing
    (empty, digits only, ``Track NN``).  A leading track number that is the
    file's own (``05 - Song``, ``5. Song``) is dropped (homelab#2544)."""
    title = (title or "").strip()
    if isinstance(track, int) and track > 0:
        title = re.sub(rf"^0*{track}\s*[-._)]\s*(?=\S)", "", title)
    return None if _GENERIC_TITLE.fullmatch(title) else title


class SpotdlTags(NamedTuple):
    url: str | None = None
    isrc: str | None = None


class Pending(NamedTuple):
    """What an inbox item's path and file said at ``import_task_created``, for
    ``item_imported`` to put back.  By ``id()``, as neither its file name nor its
    title tells it from another item by then (homelab#2707).  Holds the item,
    like :class:`FileTags`."""

    item: object
    playlist: str
    via: str
    job: str | None
    tags: SpotdlTags


def keeps_suffixed_name(current: bytes, dest: bytes) -> bool:
    """True when *dest* is held by another file and *current* is one of the
    names ``beets.util.unique_path`` picks for it (``<stem>.<n><ext>``), so a
    move should leave the item where it is (homelab#2723)."""
    if not os.path.exists(beets_util.syspath(dest)) or beets_util.samefile(current, dest):
        return False
    base, ext = os.path.splitext(os.fsencode(dest))
    base = re.sub(rb"\.\d+$", b"", base)  # unique_path counts on from a suffix dest already has
    name, current_ext = os.path.splitext(os.fsencode(current))
    return current_ext == ext and re.fullmatch(re.escape(base) + rb"\.\d+", name) is not None


def _patch_move_file() -> None:
    """Make ``Item.move_file`` keep an item at its suffixed name while its
    destination is taken (:func:`keeps_suffixed_name`).  Idempotent."""
    move_file = beets_library.Item.move_file
    if getattr(move_file, "_keeps_suffixed_name", False):
        return

    def keeping_suffixed_name(self, dest, operation=beets_util.MoveOperation.MOVE):
        if operation == beets_util.MoveOperation.MOVE and keeps_suffixed_name(self.path, dest):
            return  # already at a unique name for dest: moving would only take the next one
        return move_file(self, dest, operation)

    keeping_suffixed_name._keeps_suffixed_name = True
    keeping_suffixed_name.__wrapped__ = move_file
    beets_library.Item.move_file = keeping_suffixed_name


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


def _job_from_path(path: str | bytes) -> str | None:
    """The album-mode job folder of a usenet inbox file (``usenet/<playlist>/<job>/…``),
    which tells one release's items from another's imported in the same scan (#284)."""
    if isinstance(path, bytes):
        path = path.decode()
    try:
        parts = Path(path).relative_to(USENET_INBOX).parts
    except ValueError:
        return None
    return parts[1] if len(parts) >= 3 else None


def _all_via_spotdl(duplicates: list) -> bool:
    """Return True if every duplicate in *duplicates* is a pipeline import (spotdl or usenet)."""
    return all((item.get("via") or "") in MANAGED_VIA for item in duplicates)


class Incoming(NamedTuple):
    """Identity of the track being imported."""

    spotify_id: str | None
    isrcs: frozenset[str]
    mb_trackid: str | None
    length: float | None = None


def _find_same_recording(lib, incoming: Incoming) -> list:
    """Library items that share a Spotify track ID, ISRC or MusicBrainz recording ID,
    the last with lengths that agree (homelab#2540: beets' recording is a best guess)."""
    found: dict = {}
    queries = []
    if incoming.spotify_id:
        sid = incoming.spotify_id
        queries += [(f"spotify_ids:{sid}", lambda i: sid in item_spotify_ids(i)),
                    (f"spotify_url:{sid}", lambda i: sid in item_spotify_ids(i))]
    for code in incoming.isrcs:
        queries.append((f"isrc:{code}", lambda i, c=code: c in item_isrcs(i)))
    if incoming.mb_trackid:
        queries.append((f"mb_trackid:{incoming.mb_trackid}",
                        lambda i: same_mb_recording(i.get("mb_trackid"), i.get("length"),
                                                    incoming.mb_trackid, incoming.length)))
    for query, same in queries:
        for item in lib.items(query):
            if same(item):
                found.setdefault(item.id, item)
    return list(found.values())


def _different_recording(incoming: Incoming, dup) -> bool:
    """True when both sides have ISRCs, none shared, and no shared recording ID."""
    if same_mb_recording(incoming.mb_trackid, incoming.length, dup.get("mb_trackid"), dup.get("length")):
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
        _patch_move_file()
        # id(item) → its playlist, via, job and spotdl tags, from import_task_created to item_imported.
        self._pending: dict[int, Pending] = {}
        # id(item) → a Usenet item's own tags, from import_task_created to import_task_apply.
        self._file_tags: dict[int, FileTags] = {}
        # id(item) → its retitle, from import_task_apply to item_imported, which
        # has the item ID the event names (homelab#2590).
        self._retitled: dict[int, Retitle] = {}
        # id(item) → what it was imported beside, from import_task_choice to item_imported.
        self._beside: dict[int, list[Beside]] = {}
        self.register_listener("import_task_created", self.tag_source_on_created)
        self.register_listener("import_task_apply", self.restore_file_title)
        self.register_listener("import_task_apply", self.clear_singleton_release_track)
        self.register_listener("item_imported", self.tag_source_on_stored)
        self.register_listener("import_task_choice", self.handle_duplicates)
        self.register_listener("item_imported", self.fingerprint_if_missing)

    def tag_source_on_created(self, session, task):
        """Tag incoming tracks with source= and via= at task creation.

        Fires from handle_created() during read_tasks — always, regardless of
        autotag mode — while item.path still points to the inbox file (before
        any pipeline stage runs).

        Keeps them in _pending so tag_source_on_stored can re-apply them after
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
            job = _job_from_path(item.path)
            if job:
                item["usenet_job"] = job
            if via == "usenet":
                # Its own title goes back on at import_task_apply (homelab#2544).
                own = own_title(item.title, getattr(item, "track", None))
                self._file_tags[id(item)] = FileTags(
                    item, item.title or "", own, {f: item.get(f) for f in RECORDING_FIELDS},
                    {f: item.get(f) for f in ARTIST_FIELDS}, artist_credit(item) | artist_credit(item, album=True),
                    compilation(item), " ".join(filter(None, (item.get("album"), job))))
            tags = _read_spotdl_tags(item.path)
            if tags.url or tags.isrc:
                self._apply_spotdl_tags(item, tags)
            self._pending[id(item)] = Pending(item, playlist, via, job, tags)
            self._log.debug(
                "tagged incoming track source={} via={}: {}", playlist, via, item.path
            )

    def restore_file_title(self, session, task):
        """Put a Usenet album track's own title back over beets' match (homelab#2544).

        Fires from ``import_task_apply``: after ``apply_metadata`` wrote the
        matched recording's fields, before ``task.add`` stores the item and
        before its file is written and moved, so the title reaches the tags
        and the library path.  Keyed by the item object, which beets keeps from
        ``import_task_created`` to here (a test holds beets to it).
        """
        for item in _items_from_task(task):
            entry = self._file_tags.pop(id(item), None)
            if entry is None or entry.item is not item:
                if _via_from_path(item.path) == "usenet":
                    self._log.warning(
                        "[TITLE] {}: no file title captured at import; beets' title kept", item.title)
                continue
            matched = item.title or ""
            title = entry.own
            if title is None:
                self._log.warning(
                    "[TITLE] {}: the file's title {!r} names nothing; beets' title kept", matched, entry.title)
                continue
            disambig = item.get("trackdisambig") or ""
            recording = item.get("mb_trackid") or None
            other_artist = entry.another_artist(item)
            matched_artist = item.get("artist") or ""
            why, versions = wrong_recording(title, entry.album, matched, disambig, other_artist)
            dropped = why is not None
            if title != matched and "".join(words(title)) == "".join(words(matched)):
                # Only case, punctuation or spacing differs (In My Time of Need, In My
                # Time Of Need; Saved theEarth): no sibling, and MusicBrainz's spelling
                # is the better one.
                title = matched
            item["title"] = title
            item["trackdisambig"] = ""
            if dropped:
                for field, value in entry.fields.items():
                    item[field] = value
            if other_artist:
                for field, value in entry.artist_fields.items():
                    item[field] = value
            note = _dropped_note(why, versions, matched_artist)
            if title != matched:
                message = f"[TITLE] {matched} → {title}{note}"
            elif dropped:
                cleared = f": trackdisambig {disambig!r} cleared" if disambig else ""
                message = f"[TITLE] {title}{cleared}{note}"
            elif disambig:
                message = f"[TITLE] {title}: trackdisambig {disambig!r} cleared"
            else:
                continue
            self._log.warning("{}", message)
            self._retitled[id(item)] = Retitle(item, message, {
                "matched": matched, "file_title": entry.title, "trackdisambig": disambig or None,
                "recording_dropped": dropped, "dropped_mb_trackid": recording if dropped else None,
                "dropped_why": why, "versions": versions or None,
                "file_artist": entry.artist_fields["artist"] if other_artist else None,
                "matched_artist": matched_artist if other_artist else None,
                "usenet_job": item.get("usenet_job") or None})

    def clear_singleton_release_track(self, session, task):
        """Clear the recording ID beets writes as a singleton's ``mb_releasetrackid`` (homelab#2623).

        beets' ``TrackInfo`` maps ``release_track_id or track_id`` to
        ``mb_releasetrackid``, and a singleton match is a bare recording with no
        release track, so every pipeline import (``singletons: yes``) stored its
        recording ID there: a release track ID that names no release track, and a
        stale one once the recording is replaced (13744, 3809, 17061).  Fires
        after :meth:`restore_file_title`, so a file's own value it put back is
        judged the same way; a real release track ID (another value) stays."""
        item = getattr(task, "item", None)  # an album task (not used) has real release tracks
        if item is not None and item.get("mb_releasetrackid") and item.get("mb_releasetrackid") == item.get("mb_trackid"):
            item["mb_releasetrackid"] = ""

    def _wrong_recording(self, item, info) -> bool:
        """True when a Usenet item's own title and its match's are two songs
        (homelab#2544) or two versions (homelab#2609), or its artist and the
        match's are two artists (homelab#2649), so the match's recording isn't
        the file's: :func:`wrong_recording`."""
        entry = self._file_tags.get(id(item))
        if entry is None or entry.item is not item or entry.own is None:
            return False
        why, _ = wrong_recording(entry.own, entry.album, info.get("title") or "", info.get("trackdisambig") or "",
                                 entry.another_artist(info))
        return why is not None

    def tag_source_on_stored(self, lib, item):
        """Re-apply source= and via= after the item is persisted to the library.

        Fires from item_imported, after autotag and item.store() have run.
        MusicBrainz autotag can replace the item's metadata dict wholesale and
        beets renames the file, both of which discard the flex attributes set
        earlier by tag_source_on_created.  They are found by the item object,
        the one thing here that is still the inbox item's alone (homelab#2707).
        """
        # An as-is import never reaches import_task_apply (homelab#2544).
        self._file_tags.pop(id(item), None)
        self._record_retitle(item)
        self._record_beside(item)
        pending = self._pending.pop(id(item), None)
        if pending is None:
            return
        item["sources"] = pending.playlist
        item["via"] = pending.via
        if pending.job:
            item["usenet_job"] = pending.job
        self._apply_spotdl_tags(item, pending.tags)
        item.store()
        self._log.debug(
            "persisted source={} via={} on stored item: {}", pending.playlist, pending.via, item.path
        )
        if pending.via == "spotdl":
            # A Usenet item is one of a release: its album's [DONE] is the event.
            self._record_imported(item, pending.playlist)

    def _record_retitle(self, item) -> None:
        """The ``retitled`` event for a title :meth:`restore_file_title` put back (homelab#2590)."""
        retitle = self._retitled.pop(id(item), None)
        if retitle is None or retitle.item is not item:
            return
        events.record("retitled", retitle.message, source="import", item_id=item.id,
                      spotify_album_id=events.spotify_album(item.get("spotify_album_id")),
                      release_group=item.get("mb_releasegroupid"),
                      data={**events.item_fields(item), **retitle.data})

    def _note_beside(self, item, dup, kind: str, why: str, line: str, *args, **data) -> None:
        """Log one duplicate *item* is imported beside; its event waits for the item's ID."""
        self._log.warning(line, item.artist, item.title, dup, *args)
        self._beside.setdefault(id(item), []).append(Beside(item, kind, line.format(item.artist, item.title, dup, *args), {
            "why": why, "duplicate": {"item_id": dup.id, **events.item_fields(dup), "via": dup.get("via"),
                                      "isrc": dup.get("isrc") or None}, **data}))

    def _record_beside(self, item) -> None:
        """The ``import_alongside`` and ``kept_apart`` events for what
        :meth:`handle_duplicates` imported *item* beside (homelab#2590)."""
        for beside in self._beside.pop(id(item), ()):
            if beside.item is item:
                events.record(beside.kind, beside.message, source="import", item_id=item.id,
                              spotify_album_id=events.spotify_album(item.get("spotify_album_id")),
                              release_group=item.get("mb_releasegroupid"),
                              data={**events.item_fields(item), **beside.data})

    @staticmethod
    def _record_imported(item, playlist: str) -> None:
        """The ``imported`` event for a spotdl download now in the library (homelab#2590):
        the nightly's, an as-is import's, or an album request's verified fallback."""
        verified = item.get(FALLBACK_RECORDING) or None
        events.record(
            "imported", f"[IMPORTED] {playlist}: {item.get('artist')} — {item.get('title')}"
                        f"{f' (fallback, recording {verified} verified)' if verified else ''}",
            source="import", item_id=item.id,
            spotify_album_id=events.spotify_album(item.get("spotify_album_id")),
            release_group=item.get("mb_releasegroupid"),
            data={**events.item_fields(item), "playlist": playlist,
                  "spotify_ids": split_list(item.get("spotify_ids")) or None,
                  "isrc": item.get("isrc") or None, "mb_trackid": item.get("mb_trackid") or None,
                  "fallback_recording": verified})

    def fingerprint_if_missing(self, lib, item):
        """Fingerprint an item imported without one (asis imports, #210)."""
        if item.get("acoustid_fingerprint"):
            return
        try:
            item["acoustid_fingerprint"] = fingerprint(item.path.decode() if isinstance(item.path, bytes) else item.path)
        except Exception as exc:  # noqa: BLE001 — the import must not fail over a fingerprint
            self._log.warning("fingerprinting {} failed: {}", item.path, exc)
            return
        # Written first, so the store keeps the file's new mtime and beet update reads the tag back (homelab#2723).
        item.try_write()
        item.store()

    @staticmethod
    def _apply_spotdl_tags(item, tags: SpotdlTags) -> None:
        """Set spotify_url and spotify_ids, and add spotdl's ISRC to the item's.
        An album request's fallback download (homelab#2478) carries its
        MusicBrainz recording URL instead, which the guard verified by
        fingerprint: it sets ``fallback_recording``, never a Spotify field."""
        if recording := url_recording(tags.url):
            item[FALLBACK_RECORDING] = recording
            if not item.get("mb_trackid"):
                # AcoustID named it (the guard): an as-is import keeps the ID the retire step matches by.
                item["mb_trackid"] = recording
        elif tags.url:
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
        if self._wrong_recording(item, info):
            # The match is another song (homelab#2544), another version of it
            # (homelab#2609) or another artist's (homelab#2649): its recording and
            # ISRCs aren't the file's, and import_task_apply drops them.
            info = {}
        isrcs = {*split_list(tags.isrc, ";"), *split_list(info.get("isrc"), ";"), *item_isrcs(item)}
        mb_trackid = (info.get("track_id") or info.get("mb_trackid") or item.get("mb_trackid")
                      or url_recording(tags.url) or None)
        return Incoming(spotify_id(tags.url), frozenset(isrcs), mb_trackid, getattr(item, "length", None))

    def _incoming_spotdl_tags(self, item) -> SpotdlTags:
        """spotdl tags of an inbox item: cached at task creation, else read again."""
        pending = self._pending.get(id(item))
        return pending.tags if pending else _read_spotdl_tags(item.path)

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

        recording = url_recording(self._incoming_spotdl_tags(items[0]).url) if len(items) == 1 else None
        if same and recording and not any(d.get(FALLBACK_RECORDING) == recording for d in same):
            # An album request's fallback track (homelab#2478): its album keeps its own copy, as a
            # release does (#279), stamped with its key after the import.  A second fallback copy
            # of the recording is a duplicate like any other.
            for dup in same:
                self._note_beside(
                    items[0], dup, "import_alongside", "the album request's copy",
                    "[BOTH] {} — {}: same recording as {}; importing the album request's copy alongside",
                )
            task.find_duplicates = task.duplicate_items = lambda lib: []
            return
        usenet = same and _via_from_path(items[0].path) == "usenet"
        # Another release's item: kept apart, the release is one album (#278, #279).
        apart = [d for d in same if d.get("via") == "usenet"] if usenet else []
        if usenet and _all_via_spotdl(same):
            # The album copy wins (#266): import it, and the album completion
            # supersedes the loose copies with it, playlists and all.
            for dup in same:
                if any(dup is a for a in apart):
                    self._note_beside(
                        items[0], dup, "import_alongside", "another release's copy",
                        "[BOTH] {} — {}: same recording as {}, another release's copy; importing it alongside",
                    )
                else:
                    self._note_beside(
                        items[0], dup, "import_alongside", "the album copy supersedes it",
                        "[ALBUM] {} — {}: same recording as {}; importing the album copy, which supersedes it",
                    )
            task.find_duplicates = task.duplicate_items = lambda lib: []
            return
        if same:
            dup_items = same
        elif dup_items and incoming is not None:
            # No identifier agrees: never merge by artist+title (#255).
            for dup in dup_items:
                if _different_recording(incoming, dup):
                    self._note_beside(
                        items[0], dup, "kept_apart", "ISRCs differ",
                        "[SPLIT] {} — {}: a different recording from {} (ISRCs {} vs {}); importing it separately",
                        ",".join(sorted(incoming.isrcs)), dup.get("isrc"), isrcs=sorted(incoming.isrcs),
                    )
                else:
                    self._note_beside(
                        items[0], dup, "kept_apart", "no shared ID",
                        "[APART] {} — {}: shares no Spotify ID, ISRC or recording ID with {}; importing it separately",
                    )
            # Hide them from beets' own check, or duplicate_action: remove deletes them.
            task.find_duplicates = task.duplicate_items = lambda lib: []
            return

        if not dup_items:
            return

        if _all_via_spotdl(dup_items):
            # The incoming playlist name: the file is still at its inbox path.
            incoming_playlist = next(filter(None, (_playlist_from_path(item.path) for item in items)), None)

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
                    # Warning, like [SPLIT]/[APART]: beets drops plugin debug lines (#191).
                    self._log.warning(
                        "[SAME] {} — {}: same recording as {}; merged (playlist {})",
                        items[0].artist, items[0].title, dup, incoming_playlist,
                    )
                    events.record(
                        "merge", f"[SAME] {items[0].artist} — {items[0].title}: same recording as {dup}; "
                                 f"merged (playlist {incoming_playlist})",
                        source="import", item_id=dup.id,
                        spotify_album_id=events.spotify_album(dup.get("spotify_album_id")),
                        release_group=dup.get("mb_releasegroupid"),
                        data={**events.item_fields(dup), "why": "same recording at import",
                              "incoming": {"artist": items[0].artist, "title": items[0].title,
                                           "playlist": incoming_playlist}},
                    )
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

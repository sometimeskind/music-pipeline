"""The registry of event kinds (homelab#2618): what each records, about what, and its ``data``.

The events table is an audit log of why the library changed, not event sourcing
(homelab ``docs/media/notes/music-events-are-an-audit-log.md``): nothing
rebuilds state from it.  Its kinds are validated here, in code, not by the
database: migration 23 dropped the ``events_kind_check`` CHECK that every new
kind used to widen with a migration of its own.  :data:`KINDS` is the single
list.  ``db.EVENT_KINDS`` is its keys, :func:`music_fetch.events.record` and
:func:`~music_fetch.events.insert` refuse a kind it lacks, and the Music MCP's
``find_events`` refuses one with the known kinds listed.

A new kind is one entry here, plus the call that records it: no migration.
``service/tests/test_event_kinds.py`` reads every ``events.record`` and
``events.insert`` call in the code and fails when one records a kind missing
here, or spells out a ``data`` key its kind doesn't list.

Each kind's *subject* says what its row is about:

- ``album``: an album record.  The row carries the album's ``spotify_album_id``
  and ``release_group`` when known, and ``data.key`` (the album store's key).
- ``item``: a beets item, by ``item_id``; the item's album too when known.  Most
  item kinds carry the item's ``artist``, ``title``, ``album`` and ``path`` in
  ``data`` (``events.item_fields``).
- ``playlist``: a playlist, named by ``data.playlist``.
- ``none``: none of these (an MCP job, a hand edit of the state).

*data* lists every key the kind may carry in ``data``; most events carry some of
them.  The README and ``service/MCP_API.md`` describe the events by area and
point here for the full list.
"""

from __future__ import annotations

from typing import NamedTuple

ALBUM, ITEM, PLAYLIST, NONE = "album", "item", "playlist", "none"
SUBJECTS = (ALBUM, ITEM, PLAYLIST, NONE)

# The keys events.item_fields gives an item event's data.
_ITEM = ("artist", "title", "album", "path")
# The keys an album record's event carries (albums._event, stranded._event).
_RECORD = ("album", "key", "origin")


class Kind(NamedTuple):
    subject: str
    description: str
    data: tuple[str, ...] = ()
    # Nothing records it any more; old rows may still carry it.
    retired: bool = False


KINDS: dict[str, Kind] = {
    # --- An album-mode import (music_fetch.albums) ---
    "import_done": Kind(ALBUM, "[DONE]: an album imported; an incomplete copy kept as the album says why.",
                        (*_RECORD, "why", "reposts", "missing", "lacking", "kept")),
    "import_wrong": Kind(ALBUM, "[WRONG]: a download that is not the album asked for.",
                         (*_RECORD, "release", "found", "items", "total")),
    "import_incomplete": Kind(ALBUM, "[INCOMPLETE]: a download lacking tracks of its release.",
                              (*_RECORD, "release", "missing", "lacking", "kept", "same_gap")),
    "import_lost": Kind(ALBUM, "[LOST]: a completed job whose directory is gone.", (*_RECORD, "path")),
    # --- The release check and the duplicate tools (music_scan.release, dupes, canon) ---
    "retire": Kind(ITEM, "[RETIRE]: an item of a release the album no longer keeps, retired; the IDs it "
                         "handed over in ids_to and the taken fields.",
                   (*_ITEM, "origin", "ids_to", "spotify_ids", "sources", "isrc")),
    "local": Kind(ITEM, "[LOCAL]: an item kept as a local-only extra, or left to music-audit-dupes.",
                  (*_ITEM, "origin", "evidence", "extras", "left_to_dupes", "release_items")),
    "merge": Kind(ITEM, "An item merged into another (music-audit-dupes, or a same-recording duplicate at "
                        "import); the recording ID taken or why not.",
                  (*_ITEM, "why", "merged", "merged_item", "incoming", "mb_trackid_taken", "mb_trackid_refused")),
    "clash": Kind(ITEM, "[CLASH]: canon's move of an item onto a path another item holds (once a day).",
                  (*_ITEM, "dest", "in_place")),
    "mixed": Kind(ALBUM, "[MIXED]: an album whose items come from more than one release.", (*_RECORD, "reasons")),
    "moved": Kind(ITEM, "[RETAG]: canon moved an item from one Spotify album to another.", (*_ITEM, "from", "to")),
    # --- The album store (music_fetch.albums) ---
    "grab": Kind(ALBUM, "[GRAB]: a Usenet release sent to SABnzbd.",
                 (*_RECORD, "release", "guid", "size", "reimport")),
    "autoreset": Kind(ALBUM, "[AUTORESET]: an album reset automatically, with why.", (*_RECORD, "reasons")),
    "fallback": Kind(ALBUM, "[FALLBACK]: tracks left to the spotdl fallback.", (*_RECORD, "tracks", "left")),
    "failure": Kind(ALBUM, "[FAIL]/[ERR]/[MISS]: a grab, an album or a spotdl download given up on or backed off.",
                    (*_RECORD, "what", "reason", "release", "gave_up", "track", "url", "kind", "attempts", "days")),
    "filled": Kind(ALBUM, "[FILLED]: a fallback album the library now completes.", _RECORD),
    "fallback_deferred": Kind(ALBUM, "[DEFER]: a request album's spotdl fallback put off to the next session.",
                              (*_RECORD, "tracks", "why")),
    "search_miss": Kind(ALBUM, "[MISS]: a search that matched nothing to grab (once a day).",
                        (*_RECORD, "queries", "results", "status", "why")),
    "status_changed": Kind(ALBUM, "An album's status changed with no other event naming it.",
                           (*_RECORD, "from", "to", "why", "playlists")),
    "album_queued": Kind(ALBUM, "[QUEUED]: a feed created an album record.", (*_RECORD, "why")),
    "album_dropped": Kind(ALBUM, "[DROPPED]/[UNWANTED]: a feed dropped an album record.",
                          (*_RECORD, "status", "playlists", "why")),
    "reset": Kind(ALBUM, "[RESET]: an album reset by hand (music-album-reset, reset_album).",
                  (*_RECORD, "before", "after", "reimport")),
    "requeued": Kind(ALBUM, "[REQUEUE]: an album requeued by hand (music-album-requeue, requeue_albums).",
                     (*_RECORD, "fell_back_at", "tracks")),
    # --- Requests, Later and the resolve pass ---
    "album_requested": Kind(ALBUM, "An album requested, or a request taking over a record (data.what).",
                            (*_RECORD, "requested_by", "preferred_release", "tracklist_release", "tracks", "what",
                             "was", "whole", "playlists")),
    "later_added": Kind(ALBUM, "[LATER]: an album added to Later.",
                        ("album", "key", "ref", "available", "cutover", "moved")),
    "later_removed": Kind(ALBUM, "[LATER]: an album taken off Later.",
                          ("album", "key", "ref", "added_at", "available_at")),
    "later_resolved": Kind(ALBUM, "A Later entry resolved to its album's release group.",
                           ("album", "key", "ref", "removed_entry")),
    "later_available": Kind(ALBUM, "A Later entry whose album the library now holds whole.", ("album", "key", "ref")),
    "spotify_id_resolved": Kind(ALBUM, "[REKEY]: the resolve pass keyed a record by its release group; "
                                       "data.conflict is evidence the import outranked.",
                                ("album", "key", "via", "conflict")),
    "album_merged": Kind(ALBUM, "[MERGE]: the resolve pass merged records into one; data.conflict is what the "
                                "merge settled (an absorbed record's evidence, differing search overrides).",
                         ("album", "key", "absorbed", "conflict")),
    "conflict": Kind(ALBUM, "[CONFLICT]: evidence naming two release groups leaves a record unkeyed "
                            "(once a day while it stands).", ("album", "key", "conflict")),
    "merged_releases": Kind(ALBUM, "[MERGED]/[MULTI]: which releases a record a release-group merge left "
                                   "holding several keeps, and why.",
                            ("album", "key", "kept", "why", "releases")),
    # --- Overrides, holds, pins and hand edits of the state ---
    "override_set": Kind(ALBUM, "A search override, hold or track pin set (data.kind); a pin has no album.",
                         ("album", "key", "kind", "words", "reason", "refs", "override", "reattached",
                          "spotify_track_id", "isrc", "url", "label")),
    "override_cleared": Kind(ALBUM, "A search override, hold or track pin cleared (data.kind).",
                             ("album", "key", "kind", "words", "reason", "spotify_track_id", "isrc", "url")),
    "state_edited": Kind(NONE, "[STATE]: a hand or automatic edit of the state; data.what says which (backoff, "
                               "store, snapshot, store_tail).",
                         ("what", "why", "playlist", "tracks", "kinds", "source", "updated", "entries", "local",
                          "snapshot_id", "items")),
    # --- Recordings (music_scan.mb_fingerprint, request_fill) ---
    "recording_replaced": Kind(ITEM, "[RECHECK]: a guessed recording ID replaced by the one AcoustID names.",
                               (*_ITEM, "old", "new", "score", "keeper", "run", "refreshed")),
    "recording_refreshed": Kind(ITEM, "An item's title or trackdisambig refreshed from its replaced recording.",
                                (*_ITEM, "recording", "refreshed")),
    "recording_set": Kind(ITEM, "[SETREC]: a recording set on an item by hand (set_recording).",
                          (*_ITEM, "old", "new", "recording_title", "refreshed")),
    "recording_filled": Kind(ITEM, "[FILLED]: a request album's item given the recording AcoustID names.",
                             (*_ITEM, "key", "old", "new", "score", "track", "track_title", "item_track")),
    # --- Imports and the scan's later item changes ---
    "imported": Kind(ITEM, "[IMPORTED]: a spotdl download imported into the library.",
                     (*_ITEM, "playlist", "spotify_ids", "isrc", "mb_trackid", "fallback_recording")),
    "retitled": Kind(ITEM, "[TITLE]: a Usenet item's title or trackdisambig put back from its file at import.",
                     (*_ITEM, "matched", "file_title", "trackdisambig", "recording_dropped", "dropped_mb_trackid",
                      "dropped_why", "versions", "usenet_job")),
    "replaced": Kind(ITEM, "[REPLACE]: an item's audio replaced (replace_item).",
                     (*_ITEM, "old_source", "new_source", "old_path", "old_length", "quarantined", "forced")),
    "wrong_version": Kind(ITEM, "[WRONG]: a playlist entry's ID taken off an item that is another version.",
                          (*_ITEM, "playlist", "song_id", "song_isrc", "downloaded", "spotify_ids", "unlinked")),
    "retagged": Kind(ALBUM, "[RETAG]: canon's tag changes on one album's items (data.items, old and new).",
                     ("key", "album_id", "items")),
    "restamped": Kind(ALBUM, "[RESTAMP]: album_record stamps moved to the record's row.",
                      ("album", "key", "from", "items")),
    "purged": Kind(ITEM, "[PURGE]: a superseded file deleted from quarantine/replaced.",
                   ("artist", "title", "album", "path", "key", "only_copy", "days")),
    "kept": Kind(ITEM, "[KEEP]: a file moved by hand from quarantine/replaced to quarantine/keep, out of the "
                       "purge's reach (keep_quarantined).",
                 ("artist", "title", "album", "path", "dest")),
    "claimed": Kind(ITEM, "[CLAIM]/[UNCLAIMED]: a request's fallback track stamped with its record, or not.",
                    (*_ITEM, "recording", "claimed", "key", "keys")),
    "id_linked": Kind(ALBUM, "[IDS]: Spotify track IDs linked to a Usenet release's items.",
                      ("key", "playlist", "linked", "skipped")),
    # --- Playlists ---
    "reject": Kind(PLAYLIST, "[REJECT]: a download a pre-import check quarantined.",
                   ("playlist", "file", "reason", "detail", "dest")),
    "playlist_deleted": Kind(PLAYLIST, "[NAVIDROME]: a Navidrome playlist deleted because its .m3u is gone.",
                             ("playlist", "path", "navidrome_id", "songs")),
    "playlist_entry_added": Kind(PLAYLIST, "[STORE]: an entry joined a playlist store (data.by: who); a store's "
                                           "first write or a change of over 100 entries is one summary event.",
                                 ("playlist", "by", "key", "song_id", "isrc", "url", "artist", "title", "item_id",
                                  "path", "created", "bulk", "count", "entries")),
    "playlist_entry_removed": Kind(PLAYLIST, "[STORE]: an entry left a playlist store, as playlist_entry_added.",
                                   ("playlist", "by", "key", "song_id", "isrc", "url", "artist", "title", "item_id",
                                    "path", "created", "bulk", "count", "entries")),
    # --- The Music MCP ---
    "mcp_job": Kind(NONE, "[MCP]: a Music MCP job ended: tool, mode, caller, status.",
                    ("tool", "args", "mode", "tier", "status", "exit_code", "error", "created_at", "started_at",
                     "finished_at")),
}

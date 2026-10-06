"""Tests for pipeline.music_pipeline — pure helpers and listener methods."""

from unittest.mock import MagicMock, patch

from beets import importer as beets_importer

from music_scan.music_pipeline import (
    MusicPipelinePlugin,
    SpotdlTags,
    _all_via_spotdl,
    _playlist_from_path,
    _read_spotdl_tags,
)

URL_X = "https://open.spotify.com/track/X"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_plugin() -> MusicPipelinePlugin:
    """Instantiate MusicPipelinePlugin without beets' BeetsPlugin.__init__.

    Bypasses config loading and listener registration so listener methods can
    be called directly as plain functions.
    """
    plugin = MusicPipelinePlugin.__new__(MusicPipelinePlugin)
    plugin._log = MagicMock()
    plugin._pending_sources = {}
    plugin._pending_spotdl = {}
    plugin._pending_via = {}
    return plugin


def _item(path: str | bytes, sources: str = "", via: str = "", title: str = "") -> MagicMock:
    """Mock beets Item with a path and readable/writable flexible attributes."""
    m = MagicMock()
    m.path = path
    m.title = title
    data = {"sources": sources, "via": via}
    m.get = lambda k, default="": data.get(k, default)
    # __setitem__ tracked by MagicMock; also update data so .get() sees writes.
    def _setitem(k, v):
        data[k] = v
    m.__setitem__ = MagicMock(side_effect=_setitem)
    return m


def _dup(via: str = "spotdl", sources: str = "", spotify_ids: str = "") -> MagicMock:
    """Mock a library Item used as a duplicate (not a beets_library.Album)."""
    d = MagicMock(spec=["get", "__setitem__", "store"])  # spec=[] prevents hasattr from matching Album
    data = {"via": via, "sources": sources, "spotify_ids": spotify_ids}
    d.get = lambda k, default="": data.get(k, default)
    def _setitem(k, v):
        data[k] = v
    d.__setitem__ = MagicMock(side_effect=_setitem)
    d._data = data  # expose for assertions
    return d


def _task(choice_flag=None, item=None, items=None):
    """Build a mock import task with configurable choice_flag."""
    t = MagicMock()
    t.choice_flag = choice_flag if choice_flag is not None else beets_importer.Action.APPLY
    if item is not None:
        t.item = item
        t.items = None
    else:
        t.item = None
        t.items = items or []
    t.find_duplicates = MagicMock(return_value=[])
    t.chosen_info = MagicMock(return_value={})
    return t


# ---------------------------------------------------------------------------
# _playlist_from_path
# ---------------------------------------------------------------------------

def test_playlist_from_path_inside_inbox() -> None:
    path = "/root/Music/inbox/spotdl/my-playlist/track.m4a"
    assert _playlist_from_path(path) == "my-playlist"

def test_playlist_from_path_bytes() -> None:
    path = b"/root/Music/inbox/spotdl/jazz/track.m4a"
    assert _playlist_from_path(path) == "jazz"

def test_playlist_from_path_outside_inbox() -> None:
    assert _playlist_from_path("/root/Music/library/Artist/Album/track.m4a") is None

def test_playlist_from_path_inbox_root() -> None:
    # File directly in the inbox root (no playlist subdir) — returns None
    assert _playlist_from_path("/root/Music/inbox/spotdl") is None

def test_playlist_from_path_asis_staging() -> None:
    # ASIS temp-staging path: tracks quarantined then re-staged for --asis import
    path = "/tmp/asis-staging-eqn4jd_u/spotdl/my-playlist/Artist - Title.m4a"
    assert _playlist_from_path(path) == "my-playlist"

def test_playlist_from_path_arbitrary_spotdl_dir_rejected() -> None:
    # A 'spotdl' directory somewhere else (e.g. /var/) should not match
    assert _playlist_from_path("/var/data/spotdl/my-playlist/track.m4a") is None


# ---------------------------------------------------------------------------
# _all_via_spotdl
# ---------------------------------------------------------------------------

def _via_item(via: str) -> MagicMock:
    m = MagicMock()
    m.get = lambda key, default="": {"via": via}.get(key, default)
    return m

def test_all_via_spotdl_all_spotdl() -> None:
    assert _all_via_spotdl([_via_item("spotdl"), _via_item("spotdl")]) is True

def test_all_via_spotdl_one_manual() -> None:
    assert _all_via_spotdl([_via_item("spotdl"), _via_item("")]) is False

def test_all_via_spotdl_empty() -> None:
    assert _all_via_spotdl([]) is True  # vacuously true — guarded by `if not found` in caller

def test_all_via_spotdl_none_via() -> None:
    assert _all_via_spotdl([_via_item("")]) is False


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_created — tagging
# ---------------------------------------------------------------------------

def test_tag_source_on_created_singleton_in_inbox() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    item.__setitem__.assert_any_call("sources", "jazz")
    item.__setitem__.assert_any_call("via", "spotdl")


def test_tag_source_on_created_singleton_outside_inbox() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    item.__setitem__.assert_not_called()


def test_tag_source_on_created_album_task_tags_all_items() -> None:
    plugin = _make_plugin()
    item1 = _item("/root/Music/inbox/spotdl/pop/a.m4a")
    item2 = _item("/root/Music/inbox/spotdl/pop/b.m4a")
    task = _task(items=[item1, item2])

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    for item in (item1, item2):
        item.__setitem__.assert_any_call("sources", "pop")
        item.__setitem__.assert_any_call("via", "spotdl")


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_created — pending-source cache
# ---------------------------------------------------------------------------

def test_tag_source_on_created_caches_filename_and_title() -> None:
    """Both the inbox filename and the normalised title must be cached."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/Artist - My Track.m4a", title="My Track")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending_sources["Artist - My Track.m4a"] == "jazz"
    assert plugin._pending_sources["my track"] == "jazz"


def test_tag_source_on_created_no_title_caches_filename_only() -> None:
    """When title is empty only the filename key is stored."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a", title="")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending_sources == {"track.m4a": "jazz"}


def test_tag_source_on_created_outside_inbox_not_cached() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending_sources == {}


def test_tag_source_on_created_album_task_caches_all_items() -> None:
    plugin = _make_plugin()
    item1 = _item("/root/Music/inbox/spotdl/pop/a.m4a", title="Alpha")
    item2 = _item("/root/Music/inbox/spotdl/pop/b.m4a", title="Beta")
    task = _task(items=[item1, item2])

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending_sources["a.m4a"] == "pop"
    assert plugin._pending_sources["alpha"] == "pop"
    assert plugin._pending_sources["b.m4a"] == "pop"
    assert plugin._pending_sources["beta"] == "pop"


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_stored — item_imported re-apply
# ---------------------------------------------------------------------------

def test_tag_source_on_stored_applies_via_filename_key() -> None:
    """Filename key lookup works for items that beets didn't rename."""
    plugin = _make_plugin()
    plugin._pending_sources = {"track.m4a": "jazz"}
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("sources", "jazz")
    item.__setitem__.assert_any_call("via", "spotdl")
    item.store.assert_called_once()


def test_tag_source_on_stored_applies_via_title_key_after_rename() -> None:
    """Title key lookup resolves when beets renamed the file (the common case).

    spotdl names files "Artist - Title.m4a"; beets renames to "NN - Title.m4a".
    The filename key no longer matches, so the title key must be used.
    """
    plugin = _make_plugin()
    plugin._pending_sources = {
        "Artist Name - My Track.m4a": "jazz",  # spotdl original filename
        "my track": "jazz",                     # title key added by tag_source_on_created
    }
    # item_imported fires with the beets-renamed library path
    item = _item("/root/Music/library/Artist Name/Album/03 - My Track.m4a", title="My Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("sources", "jazz")
    item.__setitem__.assert_any_call("via", "spotdl")
    item.store.assert_called_once()


def test_tag_source_on_stored_filename_match_also_cleans_title_key() -> None:
    """When the filename key matches, the title key is also cleaned up."""
    plugin = _make_plugin()
    plugin._pending_sources = {"track.m4a": "jazz", "my track": "jazz"}
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="My Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    assert "track.m4a" not in plugin._pending_sources
    assert "my track" not in plugin._pending_sources


def test_tag_source_on_stored_title_match_leaves_stale_filename_key() -> None:
    """When the title key is the fallback match, the original spotdl filename key
    cannot be recovered from the renamed item.path and is left in the cache.
    It is harmless — _pending_sources is session-scoped and GC'd after the import.
    """
    plugin = _make_plugin()
    plugin._pending_sources = {
        "Artist Name - My Track.m4a": "jazz",
        "my track": "jazz",
    }
    item = _item("/root/Music/library/Artist Name/Album/03 - My Track.m4a", title="My Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    assert "my track" not in plugin._pending_sources
    # Original spotdl filename key cannot be cleaned — it remains (session-scoped).
    assert "Artist Name - My Track.m4a" in plugin._pending_sources


def test_tag_source_on_stored_unknown_item_does_nothing() -> None:
    """Items not originating from the spotdl inbox must be left untouched."""
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_not_called()
    item.store.assert_not_called()


def test_tag_source_on_stored_bytes_path() -> None:
    plugin = _make_plugin()
    plugin._pending_sources = {"track.m4a": "rock"}
    item = _item(b"/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("sources", "rock")
    item.store.assert_called_once()


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.handle_duplicates — duplicate handling
# ---------------------------------------------------------------------------

def test_handle_duplicates_skip_choice_skips_duplicate_check() -> None:
    """Tasks already marked SKIP should not trigger a duplicate check."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(choice_flag=beets_importer.Action.SKIP, item=item)

    plugin.handle_duplicates(session=MagicMock(), task=task)

    task.find_duplicates.assert_not_called()


def test_handle_duplicates_no_duplicates_does_not_skip() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = []

    plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_not_called()


def test_handle_duplicates_spotdl_only_appends_and_skips() -> None:
    """All-spotdl duplicates: append incoming playlist to sources, set SKIP."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="rock")
    task.find_duplicates.return_value = [dup]

    # Path.unlink(missing_ok=True) is a no-op for non-existent files; no patch needed.
    plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    assert dup._data["sources"] == "rock,jazz"


def test_handle_duplicates_manual_duplicate_sets_skip() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]  # no via = manual

    with patch("music_scan.music_pipeline.Path"):
        plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_handle_duplicates_manual_duplicate_deletes_inbox_file() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]

    with patch("music_scan.music_pipeline.Path") as mock_path:
        plugin.handle_duplicates(session=MagicMock(), task=task)

    mock_path.return_value.unlink.assert_called_once_with(missing_ok=True)


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_created — spotify_url capture
# ---------------------------------------------------------------------------


def test_tag_source_on_created_sets_spotify_url_and_caches() -> None:
    """Spotify URL read from the inbox file is set on the item and cached."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/Artist - My Track.m4a", title="My Track")
    task = _task(item=item)

    tags = SpotdlTags(URL_X, "GBUM71029604")
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=tags):
        plugin.tag_source_on_created(session=MagicMock(), task=task)

    item.__setitem__.assert_any_call("spotify_url", URL_X)
    item.__setitem__.assert_any_call("spotify_ids", "X")
    item.__setitem__.assert_any_call("isrc", "GBUM71029604")
    assert plugin._pending_spotdl["Artist - My Track.m4a"] == tags
    assert plugin._pending_spotdl["my track"] == tags


def test_tag_source_on_created_no_spotify_url_does_not_set_or_cache() -> None:
    """When no Spotify URL is in the inbox file, spotify_url is not set."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a", title="Track")
    task = _task(item=item)

    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags()):
        plugin.tag_source_on_created(session=MagicMock(), task=task)

    written_keys = [c[0][0] for c in item.__setitem__.call_args_list]
    assert "spotify_url" not in written_keys
    assert "spotify_ids" not in written_keys
    assert plugin._pending_spotdl == {}


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_stored — spotify_url persistence
# ---------------------------------------------------------------------------


def test_tag_source_on_stored_persists_spotify_url_via_title_key() -> None:
    """spotify_url is persisted via the title key (common case after beets renames the file)."""
    plugin = _make_plugin()
    plugin._pending_sources = {
        "Artist - My Track.m4a": "jazz",
        "my track": "jazz",
    }
    plugin._pending_spotdl = {
        "Artist - My Track.m4a": SpotdlTags(URL_X),
        "my track": SpotdlTags(URL_X),
    }
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("spotify_url", URL_X)
    item.__setitem__.assert_any_call("spotify_ids", "X")
    item.store.assert_called_once()


def test_tag_source_on_stored_persists_spotify_url_via_filename_key() -> None:
    """spotify_url is persisted via the filename key when the file was not renamed."""
    plugin = _make_plugin()
    plugin._pending_sources = {"track.m4a": "jazz"}
    plugin._pending_spotdl = {"track.m4a": SpotdlTags("https://open.spotify.com/track/Y")}
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("spotify_url", "https://open.spotify.com/track/Y")
    item.store.assert_called_once()


def test_tag_source_on_stored_no_spotify_url_does_not_set() -> None:
    """When no spotify_url was cached (e.g. file had no WOAS tag), it is not written."""
    plugin = _make_plugin()
    plugin._pending_sources = {"track.m4a": "jazz"}
    # _pending_spotdl intentionally empty
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    written_keys = [c[0][0] for c in item.__setitem__.call_args_list]
    assert "spotify_url" not in written_keys
    item.store.assert_called_once()


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.handle_duplicates — multi-playlist membership append
# ---------------------------------------------------------------------------


def test_handle_duplicates_appends_to_sources_via_pending() -> None:
    """Existing spotdl item gets incoming playlist appended via _pending_sources."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    plugin._pending_sources["song"] = "playlist-b"
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a")
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    assert dup._data["sources"] == "playlist-a,playlist-b"
    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    dup.store.assert_called_once()


def test_handle_duplicates_appends_idempotent() -> None:
    """Appending a playlist that already exists in sources is a no-op."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    plugin._pending_sources["song"] = "playlist-b"
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a,playlist-b")
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    assert dup._data["sources"] == "playlist-a,playlist-b"
    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    dup.store.assert_not_called()


def test_handle_duplicates_fallback_to_path() -> None:
    """When _pending_sources misses, playlist is resolved from the inbox path."""
    plugin = _make_plugin()
    # _pending_sources intentionally empty — force path-based lookup
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a")
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    assert dup._data["sources"] == "playlist-a,playlist-b"
    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_handle_duplicates_both_lookups_miss_falls_through() -> None:
    """When playlist cannot be resolved, falls through to duplicate_action with a warning."""
    plugin = _make_plugin()
    # Path is outside the spotdl inbox — _playlist_from_path returns None.
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a")
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_not_called()
    plugin._log.warning.assert_called()


def test_handle_duplicates_file_deletion_failure_still_skips() -> None:
    """An OSError from Path.unlink does not prevent SKIP from being set."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    plugin._pending_sources["song"] = "playlist-b"
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a")
    task.find_duplicates.return_value = [dup]

    with patch("music_scan.music_pipeline.Path") as mock_path:
        mock_path.return_value.name = "Song.m4a"
        mock_path.return_value.unlink.side_effect = OSError("permission denied")
        plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


# ---------------------------------------------------------------------------
# Album mode (#168): the usenet inbox
# ---------------------------------------------------------------------------


def test_playlist_from_path_usenet_inbox() -> None:
    path = "/root/Music/inbox/usenet/later/Artist-Album-FLAC/01-track.flac"
    assert _playlist_from_path(path) == "later"


def test_via_from_path() -> None:
    from music_scan.music_pipeline import _via_from_path

    assert _via_from_path("/root/Music/inbox/usenet/later/job/01.flac") == "usenet"
    assert _via_from_path(b"/root/Music/inbox/spotdl/later/a.m4a") == "spotdl"


def test_usenet_duplicates_count_as_managed() -> None:
    def dup(via):
        item = MagicMock()
        item.get.side_effect = lambda k, d=None: via if k == "via" else d
        return item

    assert _all_via_spotdl([dup("spotdl"), dup("usenet")])
    assert not _all_via_spotdl([dup("usenet"), dup("")])


def test_usenet_import_keeps_via_usenet_through_the_rename() -> None:
    """created caches via=usenet; stored re-applies it after beets renames the file."""
    plugin = _make_plugin()
    incoming = _item("/root/Music/inbox/usenet/later/Artist-Album-FLAC/01-artist-my_track.flac", title="My Track")
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags()):
        plugin.tag_source_on_created(session=MagicMock(), task=_task(item=incoming))
    incoming.__setitem__.assert_any_call("via", "usenet")

    stored = _item("/root/Music/library/Artist/Album/01 - My Track.flac", title="My Track")
    plugin.tag_source_on_stored(lib=MagicMock(), item=stored)
    stored.__setitem__.assert_any_call("sources", "later")
    stored.__setitem__.assert_any_call("via", "usenet")


# ---------------------------------------------------------------------------
# Spotify track identity (#176): spotify_ids and isrc
# ---------------------------------------------------------------------------


def test_read_spotdl_tags_reads_url_and_isrc_atoms() -> None:
    mp4 = MagicMock(tags={"----:spotdl:WOAS": [URL_X.encode()], "----:spotdl:ISRC": [b"GBUM71029604"]})
    with patch("mutagen.mp4.MP4", return_value=mp4):
        assert _read_spotdl_tags(b"/root/Music/inbox/spotdl/jazz/t.m4a") == SpotdlTags(URL_X, "GBUM71029604")


def test_read_spotdl_tags_unreadable_file_is_empty() -> None:
    with patch("mutagen.mp4.MP4", side_effect=Exception("not an mp4")):
        assert _read_spotdl_tags("/root/Music/inbox/usenet/jazz/t.flac") == SpotdlTags()


def test_tag_source_on_stored_sets_isrc_when_musicbrainz_did_not() -> None:
    plugin = _make_plugin()
    plugin._pending_sources = {"my track": "jazz"}
    plugin._pending_spotdl = {"my track": SpotdlTags(URL_X, "GBUM71029604")}
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("isrc", "GBUM71029604")


def test_tag_source_on_stored_adds_spotdl_isrc_to_musicbrainz_ones() -> None:
    """MusicBrainz's ISRCs (all of the recording's, ;-joined) and spotdl's are unioned."""
    plugin = _make_plugin()
    plugin._pending_sources = {"my track": "jazz"}
    plugin._pending_spotdl = {"my track": SpotdlTags(URL_X, "GBUM71029604")}
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")
    item["isrc"] = "USUM70000001"
    plugin.tag_source_on_stored(lib=MagicMock(), item=item)
    assert item.get("isrc") == "USUM70000001;GBUM71029604"

    # Already listed: nothing to add.
    plugin._pending_sources = {"my track": "jazz"}
    plugin._pending_spotdl = {"my track": SpotdlTags(URL_X, "GBUM71029604")}
    item.__setitem__.reset_mock()
    plugin.tag_source_on_stored(lib=MagicMock(), item=item)
    assert "isrc" not in [c[0][0] for c in item.__setitem__.call_args_list]


def test_handle_duplicates_appends_incoming_spotify_id() -> None:
    """The single's track ID joins the album track's on the one library item."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    plugin._pending_sources["song"] = "playlist-b"
    plugin._pending_spotdl["song"] = SpotdlTags("https://open.spotify.com/track/SINGLE")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a", spotify_ids="ALBUM")
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    assert dup._data["sources"] == "playlist-a,playlist-b"
    assert dup._data["spotify_ids"] == "ALBUM,SINGLE"
    dup.store.assert_called_once()


def test_handle_duplicates_stores_new_id_when_source_already_present() -> None:
    """A second entry on the same playlist still records its ID."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-a/Song.m4a", title="Song")
    plugin._pending_sources["song"] = "playlist-a"
    plugin._pending_spotdl["song"] = SpotdlTags("https://open.spotify.com/track/SINGLE")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a", spotify_ids="ALBUM")
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    assert dup._data["spotify_ids"] == "ALBUM,SINGLE"
    dup.store.assert_called_once()


def test_handle_duplicates_reads_spotify_id_from_file_on_cache_miss() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a", spotify_ids="X")
    task.find_duplicates.return_value = [dup]

    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags(URL_X)):
        plugin.handle_duplicates(session=MagicMock(), task=task)

    assert dup._data["spotify_ids"] == "X"
    assert dup._data["sources"] == "playlist-a,playlist-b"


# ---------------------------------------------------------------------------
# handle_duplicates — identity first, split different recordings (#176)
# ---------------------------------------------------------------------------


def _lib_dup(id_: int, via: str = "spotdl", **data) -> MagicMock:
    d = _dup(via=via, sources=data.pop("sources", "playlist-a"), spotify_ids=data.pop("spotify_ids", ""))
    d._data.update(data)
    d.id = id_
    return d


def _session(by_query: dict) -> MagicMock:
    """A session whose lib.items(query) returns by_query's items for that query."""
    session = MagicMock()
    session.lib.items.side_effect = lambda q: by_query.get(q, [])
    return session


def _incoming(tags: SpotdlTags = SpotdlTags(), title: str = "Song") -> tuple:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title=title)
    plugin._pending_sources[title.lower()] = "playlist-b"
    plugin._pending_spotdl[title.lower()] = tags
    return plugin, item, _task(item=item)


def test_handle_duplicates_merges_a_retitled_release_by_isrc() -> None:
    """Beets' artist+title check misses "Song - 2011 Remaster"; the shared ISRC catches it."""
    plugin, item, task = _incoming(SpotdlTags("https://open.spotify.com/track/REM", "GBX1"), title="Song - 2011 Remaster")
    existing = _lib_dup(7, isrc="USX9;GBX1", spotify_ids="ALBUM")
    session = _session({"isrc:GBX1": [existing]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    assert existing._data["sources"] == "playlist-a,playlist-b"
    assert existing._data["spotify_ids"] == "ALBUM,REM"
    assert existing._data["isrc"] == "USX9;GBX1"


def test_handle_duplicates_merges_by_musicbrainz_recording_id() -> None:
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBNEW"))
    task.chosen_info.return_value = {"track_id": "mb-rec-1", "isrc": None}
    existing = _lib_dup(7, isrc="USOLD", mb_trackid="mb-rec-1")
    session = _session({"mb_trackid:mb-rec-1": [existing]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    # Same recording: Spotify's ISRC joins MusicBrainz's.
    assert existing._data["isrc"] == "USOLD;GBNEW"
    # One warning-level line per merge (#191).
    same = [c for c in plugin._log.warning.call_args_list if "[SAME]" in c.args[0]]
    assert len(same) == 1
    assert "playlist-b" in same[0].args


def test_handle_duplicates_splits_a_different_recording() -> None:
    """Live take with the studio's artist+title: imported alongside, not merged or replaced."""
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBLIVE"))
    studio = _lib_dup(7, isrc="GBSTUDIO")
    task.find_duplicates.return_value = [studio]

    plugin.handle_duplicates(session=_session({}), task=task)

    task.set_choice.assert_not_called()
    assert studio._data["sources"] == "playlist-a"
    # beets' own duplicate check (duplicate_action: remove) no longer sees it.
    assert task.find_duplicates(MagicMock()) == []
    assert task.duplicate_items(MagicMock()) == []
    assert any("[SPLIT]" in c.args[0] for c in plugin._log.warning.call_args_list)


def test_handle_duplicates_without_isrcs_merges_by_words_and_logs() -> None:
    plugin, item, task = _incoming(SpotdlTags(URL_X))
    old = _lib_dup(7)
    task.find_duplicates.return_value = [old]

    plugin.handle_duplicates(session=_session({}), task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    assert old._data["sources"] == "playlist-a,playlist-b"
    assert any("[WORDS]" in c.args[0] for c in plugin._log.warning.call_args_list)


def test_handle_duplicates_same_recording_id_is_never_split() -> None:
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBA"))
    task.chosen_info.return_value = {"track_id": "mb-1"}
    dup = _lib_dup(7, isrc="GBB", mb_trackid="mb-1")
    session = _session({"mb_trackid:mb-1": [dup]})
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_handle_duplicates_protects_a_manual_import_matched_by_identity() -> None:
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBA"))
    manual = _lib_dup(7, via="", isrc="GBA")
    session = _session({"isrc:GBA": [manual]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    assert manual._data["sources"] == "playlist-a"


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.fingerprint_if_missing — asis imports (#210)
# ---------------------------------------------------------------------------

def test_fingerprint_if_missing_fingerprints_an_item_without_one() -> None:
    plugin = _make_plugin()
    item = _item(b"/root/Music/library/A/B/01 - T.m4a")
    with patch("music_scan.music_pipeline.fingerprint", return_value="FP") as fp:
        plugin.fingerprint_if_missing(lib=MagicMock(), item=item)
    fp.assert_called_once_with("/root/Music/library/A/B/01 - T.m4a")
    assert item.get("acoustid_fingerprint") == "FP"
    item.store.assert_called_once()
    item.write.assert_not_called()


def test_fingerprint_if_missing_leaves_chromas_fingerprint() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/A/B/01 - T.m4a")
    item["acoustid_fingerprint"] = "CHROMA"
    with patch("music_scan.music_pipeline.fingerprint") as fp:
        plugin.fingerprint_if_missing(lib=MagicMock(), item=item)
    fp.assert_not_called()
    item.store.assert_not_called()


def test_fingerprint_if_missing_never_fails_the_import() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/A/B/01 - T.m4a")
    with patch("music_scan.music_pipeline.fingerprint", side_effect=OSError("fpcalc failed")):
        plugin.fingerprint_if_missing(lib=MagicMock(), item=item)
    item.store.assert_not_called()
    plugin._log.warning.assert_called_once()

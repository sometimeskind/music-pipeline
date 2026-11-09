"""Tests for pipeline.music_pipeline — pure helpers and listener methods."""

from unittest.mock import MagicMock, patch

from beets import importer as beets_importer

from music_scan.music_pipeline import (
    MusicPipelinePlugin,
    Pending,
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
    plugin._pending = {}
    plugin._file_tags = {}
    plugin._retitled = {}
    plugin._beside = {}
    plugin._unwritten = set()
    return plugin


def _item(path: str | bytes, sources: str = "", via: str = "", title: str = "") -> MagicMock:
    """Mock beets Item with a path and readable/writable flexible attributes."""
    m = MagicMock()
    m.id = 0  # item_imported's item is stored: its events name it
    m.path = path
    m.title = title
    data = {"sources": sources, "via": via}
    m.get = lambda k, default="": data.get(k, default)
    # __setitem__ tracked by MagicMock; also update data so .get() sees writes.
    def _setitem(k, v):
        data[k] = v
    m.__setitem__ = MagicMock(side_effect=_setitem)
    return m


def _pend(plugin, item, playlist: str, tags: SpotdlTags = SpotdlTags(), via: str = "spotdl") -> None:
    """What tag_source_on_created keeps for *item*."""
    plugin._pending[id(item)] = Pending(item, playlist, via, None, tags)


def _dup(via: str = "spotdl", sources: str = "", spotify_ids: str = "") -> MagicMock:
    """Mock a library Item used as a duplicate (not a beets_library.Album)."""
    d = MagicMock(spec=["get", "__setitem__", "store", "id"])  # spec=[] prevents hasattr from matching Album
    d.id = 0  # a library item always has one; a merge's event names it
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

def test_tag_source_on_created_keeps_the_playlist_by_item() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/Artist - My Track.m4a", title="My Track")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending[id(item)].playlist == "jazz"


def test_tag_source_on_created_outside_inbox_not_cached() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a")
    task = _task(item=item)

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending == {}


def test_tag_source_on_created_album_task_caches_all_items() -> None:
    plugin = _make_plugin()
    item1 = _item("/root/Music/inbox/spotdl/pop/a.m4a", title="Alpha")
    item2 = _item("/root/Music/inbox/spotdl/pop/b.m4a", title="Beta")
    task = _task(items=[item1, item2])

    plugin.tag_source_on_created(session=MagicMock(), task=task)

    assert plugin._pending[id(item1)].playlist == "pop"
    assert plugin._pending[id(item2)].playlist == "pop"


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_stored — item_imported re-apply
# ---------------------------------------------------------------------------

def test_tag_source_on_stored_applies_what_the_item_was_read_with() -> None:
    """Found by the item, whatever beets renamed the file to."""
    plugin = _make_plugin()
    # item_imported fires with the beets-renamed library path
    item = _item("/root/Music/library/Artist Name/Album/03 - My Track.m4a", title="My Track")
    _pend(plugin, item, "jazz")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("sources", "jazz")
    item.__setitem__.assert_any_call("via", "spotdl")
    item.store.assert_called_once()
    assert plugin._pending == {}


def test_tag_source_on_stored_unknown_item_does_nothing() -> None:
    """Items not originating from the spotdl inbox must be left untouched."""
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_not_called()
    item.store.assert_not_called()


def _handle_same(plugin, session, task) -> None:
    """Run the hook with beets' duplicates also the same recording by identity:
    the merge mechanics below need an ID match, since title words alone never
    merge (#255)."""
    with patch("music_scan.music_pipeline._find_same_recording",
               side_effect=lambda lib, incoming: list(task.find_duplicates.return_value)):
        plugin.handle_duplicates(session=session, task=task)


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
    _handle_same(plugin, MagicMock(), task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    assert dup._data["sources"] == "rock,jazz"


def test_handle_duplicates_manual_duplicate_sets_skip() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]  # no via = manual

    with patch("music_scan.music_pipeline.Path"):
        _handle_same(plugin, MagicMock(), task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_handle_duplicates_manual_duplicate_deletes_inbox_file() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]

    with patch("music_scan.music_pipeline.Path") as mock_path:
        _handle_same(plugin, MagicMock(), task)

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
    assert plugin._pending[id(item)].tags == tags


def test_tag_source_on_created_no_spotify_url_does_not_set() -> None:
    """When no Spotify URL is in the inbox file, spotify_url is not set."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a", title="Track")
    task = _task(item=item)

    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags()):
        plugin.tag_source_on_created(session=MagicMock(), task=task)

    written_keys = [c[0][0] for c in item.__setitem__.call_args_list]
    assert "spotify_url" not in written_keys
    assert "spotify_ids" not in written_keys


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source_on_stored — spotify_url persistence
# ---------------------------------------------------------------------------


def test_tag_source_on_stored_persists_spotify_url() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")
    _pend(plugin, item, "jazz", SpotdlTags(URL_X))

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("spotify_url", URL_X)
    item.__setitem__.assert_any_call("spotify_ids", "X")
    item.store.assert_called_once()


def test_tag_source_on_stored_no_spotify_url_does_not_set() -> None:
    """When no spotify_url was cached (e.g. file had no WOAS tag), it is not written."""
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")
    _pend(plugin, item, "jazz")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    written_keys = [c[0][0] for c in item.__setitem__.call_args_list]
    assert "spotify_url" not in written_keys
    item.store.assert_called_once()


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.handle_duplicates — multi-playlist membership append
# ---------------------------------------------------------------------------


def test_handle_duplicates_appends_idempotent() -> None:
    """Appending a playlist that already exists in sources is a no-op."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a,playlist-b")
    task.find_duplicates.return_value = [dup]

    _handle_same(plugin, MagicMock(), task)

    assert dup._data["sources"] == "playlist-a,playlist-b"
    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    dup.store.assert_not_called()


def test_handle_duplicates_appends_the_inbox_paths_playlist() -> None:
    """Existing spotdl item gets the incoming file's playlist appended."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a")
    task.find_duplicates.return_value = [dup]

    _handle_same(plugin, MagicMock(), task)

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
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a")
    task.find_duplicates.return_value = [dup]

    with patch("music_scan.music_pipeline.Path.unlink", side_effect=OSError("permission denied")):
        _handle_same(plugin, MagicMock(), task)

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
    """created keeps via=usenet; stored re-applies it after beets renames the file."""
    plugin = _make_plugin()
    incoming = _item("/root/Music/inbox/usenet/later/Artist-Album-FLAC/01-artist-my_track.flac", title="My Track")
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags()):
        plugin.tag_source_on_created(session=MagicMock(), task=_task(item=incoming))
    incoming.__setitem__.assert_any_call("via", "usenet")

    incoming.__setitem__.reset_mock()
    incoming.path = "/root/Music/library/Artist/Album/01 - My Track.flac"
    plugin.tag_source_on_stored(lib=MagicMock(), item=incoming)
    incoming.__setitem__.assert_any_call("sources", "later")
    incoming.__setitem__.assert_any_call("via", "usenet")


def test_job_from_path() -> None:
    from music_scan.music_pipeline import _job_from_path

    assert _job_from_path(b"/root/Music/inbox/usenet/keep/Artist-Album-FLAC/CD1/01.flac") == "Artist-Album-FLAC"
    assert _job_from_path("/root/Music/inbox/usenet/keep/01.flac") is None
    assert _job_from_path("/root/Music/inbox/spotdl/keep/a.m4a") is None


def test_usenet_import_keeps_its_job_folder_through_the_rename() -> None:
    """Two releases imported in one scan stay apart: each item keeps its job folder (#284)."""
    plugin = _make_plugin()
    mine = _item("/root/Music/inbox/usenet/keep/Los Campesinos-Hello Sadness-2011-FNT/04-life.mp3",
                 title="Life Is a Long Time")
    theirs = _item("/root/Music/inbox/usenet/keep/Los Campesinos-We Are Beautiful-2009/02-miserabilia.mp3",
                   title="Miserabilia")
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags()):
        for incoming in (mine, theirs):
            plugin.tag_source_on_created(session=MagicMock(), task=_task(item=incoming))
    mine.__setitem__.assert_any_call("usenet_job", "Los Campesinos-Hello Sadness-2011-FNT")

    theirs.__setitem__.reset_mock()
    theirs.path = "/root/Music/library/Los Campesinos!/We Are Beautiful/02 - Miserabilia.mp3"
    plugin.tag_source_on_stored(lib=MagicMock(), item=theirs)
    theirs.__setitem__.assert_any_call("usenet_job", "Los Campesinos-We Are Beautiful-2009")


def test_two_releases_with_one_title_keep_their_own_job_folders() -> None:
    """The first stored used to take the last read's job folder (homelab#2707)."""
    plugin = _make_plugin()
    mine = _item("/root/Music/inbox/usenet/keep/Artist-First-2011/04-home.mp3", title="Home")
    theirs = _item("/root/Music/inbox/usenet/keep/Other-Second-2009/02-home.mp3", title="Home")
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags()):
        for incoming in (mine, theirs):
            plugin.tag_source_on_created(session=MagicMock(), task=_task(item=incoming))

    mine.path = "/root/Music/library/Artist/First/04 - Home.mp3"
    plugin.tag_source_on_stored(lib=MagicMock(), item=mine)

    assert mine.get("usenet_job") == "Artist-First-2011"


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
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")
    _pend(plugin, item, "jazz", SpotdlTags(URL_X, "GBUM71029604"))

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    item.__setitem__.assert_any_call("isrc", "GBUM71029604")


def test_tag_source_on_stored_adds_spotdl_isrc_to_musicbrainz_ones() -> None:
    """MusicBrainz's ISRCs (all of the recording's, ;-joined) and spotdl's are unioned."""
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")
    _pend(plugin, item, "jazz", SpotdlTags(URL_X, "GBUM71029604"))
    item["isrc"] = "USUM70000001"
    plugin.tag_source_on_stored(lib=MagicMock(), item=item)
    assert item.get("isrc") == "USUM70000001;GBUM71029604"

    # Already listed: nothing to add.
    _pend(plugin, item, "jazz", SpotdlTags(URL_X, "GBUM71029604"))
    item.__setitem__.reset_mock()
    plugin.tag_source_on_stored(lib=MagicMock(), item=item)
    assert "isrc" not in [c[0][0] for c in item.__setitem__.call_args_list]


def test_handle_duplicates_appends_incoming_spotify_id() -> None:
    """The single's track ID joins the album track's on the one library item."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    _pend(plugin, item, "playlist-b", SpotdlTags("https://open.spotify.com/track/SINGLE"))
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a", spotify_ids="ALBUM")
    task.find_duplicates.return_value = [dup]

    _handle_same(plugin, MagicMock(), task)

    assert dup._data["sources"] == "playlist-a,playlist-b"
    assert dup._data["spotify_ids"] == "ALBUM,SINGLE"
    dup.store.assert_called_once()


def test_handle_duplicates_stores_new_id_when_source_already_present() -> None:
    """A second entry on the same playlist still records its ID."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-a/Song.m4a", title="Song")
    _pend(plugin, item, "playlist-a", SpotdlTags("https://open.spotify.com/track/SINGLE"))
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a", spotify_ids="ALBUM")
    task.find_duplicates.return_value = [dup]

    _handle_same(plugin, MagicMock(), task)

    assert dup._data["spotify_ids"] == "ALBUM,SINGLE"
    dup.store.assert_called_once()


def test_handle_duplicates_reads_spotify_id_from_file_on_cache_miss() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/playlist-b/Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="playlist-a", spotify_ids="X")
    task.find_duplicates.return_value = [dup]

    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags(URL_X)):
        _handle_same(plugin, MagicMock(), task)

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
    _pend(plugin, item, "playlist-b", tags)
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


def test_handle_duplicates_merges_by_musicbrainz_recording_id(monkeypatch) -> None:
    from music_fetch import events

    recorded = []
    monkeypatch.setattr(events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
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
    # And one merge event (homelab#2413), which the digest checks for a wrong merge.
    (kind, message, kw), = recorded
    assert kind == "merge" and message.startswith("[SAME] ") and "(playlist playlist-b)" in message
    assert kw["item_id"] == 7 and kw["source"] == "import" and kw["data"]["why"] == "same recording at import"
    assert kw["data"]["incoming"]["playlist"] == "playlist-b"


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


def test_handle_duplicates_never_merges_by_words_alone() -> None:
    """Studio "Girlfriend Is Better" vs the live take, no ISRC on one side: imported
    alongside, never merged into it (#255)."""
    plugin, item, task = _incoming(SpotdlTags(URL_X), title="Girlfriend Is Better")
    live = _lib_dup(2530, isrc="USWB19900873")
    task.find_duplicates.return_value = [live]

    plugin.handle_duplicates(session=_session({}), task=task)

    task.set_choice.assert_not_called()
    assert live._data["sources"] == "playlist-a"
    assert live._data["spotify_ids"] == ""
    assert task.find_duplicates(MagicMock()) == []
    assert any("[APART]" in c.args[0] for c in plugin._log.warning.call_args_list)
    assert not any("[WORDS]" in c.args[0] for c in plugin._log.warning.call_args_list)

def test_handle_duplicates_same_recording_id_is_never_split() -> None:
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBA"))
    task.chosen_info.return_value = {"track_id": "mb-1"}
    dup = _lib_dup(7, isrc="GBB", mb_trackid="mb-1")
    session = _session({"mb_trackid:mb-1": [dup]})
    task.find_duplicates.return_value = [dup]

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_handle_duplicates_a_recording_id_at_another_length_is_not_the_same_recording() -> None:
    """homelab#2540: beets names a singleton's recording by a best guess, so a stereo
    download can share a 5.1 mix's ID.  11 s apart, it is imported, never skipped."""
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBA"))
    item.length = 445.2
    task.chosen_info.return_value = {"track_id": "mb-1"}
    surround = _lib_dup(7, isrc="GBB", mb_trackid="mb-1", length=456.0)
    session = _session({"mb_trackid:mb-1": [surround]})
    task.find_duplicates.return_value = [surround]

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    assert surround._data["sources"] == "playlist-a"
    assert any("[SPLIT]" in c.args[0] for c in plugin._log.warning.call_args_list)


def test_handle_duplicates_a_recording_id_within_two_seconds_is_the_same_recording() -> None:
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBA"))
    item.length = 445.2
    task.chosen_info.return_value = {"track_id": "mb-1"}
    remaster = _lib_dup(7, isrc="GBB", mb_trackid="mb-1", length=446.4)
    session = _session({"mb_trackid:mb-1": [remaster]})

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
    # The tag first, then the store keeps the file's new mtime (homelab#2723).
    assert [c[0] for c in item.method_calls if c[0] in ("try_write", "store")] == ["try_write", "store"]


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


def test_handle_duplicates_imports_the_usenet_album_copy_over_a_spotdl_one() -> None:
    """The album copy wins (#266): imported alongside, superseded after the import."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/usenet/keep/Bob Dylan-Blood On The Tracks/04.flac", title="Simple Twist Of Fate")
    task = _task(item=item)
    task.chosen_info.return_value = {"track_id": "mb-rec-1", "isrc": None}
    existing = _lib_dup(7, mb_trackid="mb-rec-1", sources="aaaaaah")
    session = _session({"mb_trackid:mb-rec-1": [existing]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    assert task.find_duplicates(session.lib) == []  # hidden from duplicate_action: remove
    assert existing._data["sources"] == "aaaaaah"
    assert any("[ALBUM]" in c.args[0] for c in plugin._log.warning.call_args_list)


def test_handle_duplicates_never_merges_a_usenet_copy_into_an_earlier_album_copy() -> None:
    """#279: an earlier copy of the album isn't merged into; the completion keeps one copy."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/usenet/later/Bob Dylan-Blood On The Tracks/04.flac", title="Simple Twist Of Fate")
    task = _task(item=item)
    task.chosen_info.return_value = {"track_id": "mb-rec-1", "isrc": None}
    existing = _lib_dup(7, via="usenet", mb_trackid="mb-rec-1", sources="keep")
    session = _session({"mb_trackid:mb-rec-1": [existing]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    assert task.find_duplicates(session.lib) == []
    assert existing._data["sources"] == "keep"
    assert any("[BOTH]" in c.args[0] for c in plugin._log.warning.call_args_list)


# ---------------------------------------------------------------------------
# handle_duplicates — a Usenet release stays one album (#278)
# ---------------------------------------------------------------------------

MELLON = "/root/Music/inbox/usenet/keep/The Smashing Pumpkins-Mellon Collie And The Infinite Sadness-24-96-WEB-FLAC-REMASTERED-2013-OBZEN"


def _release_item(path: str, title: str, album: str) -> MagicMock:
    item = _item(path, title=title)
    item["album"] = album
    return item


def test_handle_duplicates_keeps_a_release_track_that_another_album_holds() -> None:
    """2026-10-11: Mellon Collie's *1979* is the same recording as a best-of's: each album keeps its copy."""
    plugin = _make_plugin()
    item = _release_item(f"{MELLON}/205-1979.flac", "1979", "Mellon Collie and the Infinite Sadness (Remastered 2012)")
    task = _task(item=item)
    task.chosen_info.return_value = {"track_id": "mb-1979", "isrc": None}
    best_of = _lib_dup(1979, via="usenet", mb_trackid="mb-1979", sources="keep",
                       album="(Rotten Apples) The Smashing Pumpkins Greatest Hits")
    session = _session({"mb_trackid:mb-1979": [best_of]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    assert task.find_duplicates(session.lib) == []  # hidden from duplicate_action: remove
    assert best_of._data["sources"] == "keep"
    lines = [c.args[0] for c in plugin._log.warning.call_args_list]
    assert any("[BOTH]" in line for line in lines) and not any("[SAME]" in line for line in lines)


def test_handle_duplicates_never_merges_two_files_of_one_release() -> None:
    """2026-10-11 22:26: beets matched the release's *Love* to *Bodies*' recording, and
    the real *Bodies* then merged into it: two files of one import are never one track."""
    plugin = _make_plugin()
    album = "Mellon Collie and the Infinite Sadness"
    love_as_bodies = _lib_dup(3008, via="usenet", mb_trackid="mb-bodies", sources="keep", album=album)
    item = _release_item(f"{MELLON}/202-bodies_(remastered_2012).flac", "Bodies (remastered 2012)", album)
    task = _task(item=item)
    task.chosen_info.return_value = {"track_id": "mb-bodies", "isrc": None}
    session = _session({"mb_trackid:mb-bodies": [love_as_bodies]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    assert task.find_duplicates(session.lib) == []
    assert any("[BOTH]" in c.args[0] for c in plugin._log.warning.call_args_list)


def test_handle_duplicates_keeps_apart_and_supersede_takes_the_spotdl_copy() -> None:
    """Another album's release copy stays, and a loose spotdl copy is left to supersede (#266)."""
    plugin = _make_plugin()
    item = _release_item(f"{MELLON}/205-1979.flac", "1979", "Mellon Collie and the Infinite Sadness")
    task = _task(item=item)
    task.chosen_info.return_value = {"track_id": "mb-1979", "isrc": None}
    best_of = _lib_dup(1979, via="usenet", mb_trackid="mb-1979", album="Greatest Hits")
    loose = _lib_dup(12, via="spotdl", mb_trackid="mb-1979", sources="aaaaaah", album="1979")
    session = _session({"mb_trackid:mb-1979": [best_of, loose]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    lines = [c.args[0] for c in plugin._log.warning.call_args_list]
    assert any("[BOTH]" in line for line in lines) and any("[ALBUM]" in line for line in lines)




# ---------------------------------------------------------------------------
# An album request's fallback download (homelab#2478)
# ---------------------------------------------------------------------------

REC = "00000001-0000-4000-8000-000000000001"
REC_URL = f"https://musicbrainz.org/recording/{REC}"


def test_a_request_fallback_download_records_its_recording_never_a_spotify_id() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/request/Band - Song.m4a", title="Song")
    task = _task(item=item)
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags(REC_URL, "GBAAA001001")):
        plugin.tag_source_on_created(session=MagicMock(), task=task)
    item.__setitem__.assert_any_call("fallback_recording", REC)
    item.__setitem__.assert_any_call("mb_trackid", REC)
    item.__setitem__.assert_any_call("sources", "request")
    keys = [c.args[0] for c in item.__setitem__.call_args_list]
    assert "spotify_url" not in keys and "spotify_ids" not in keys


def test_a_request_fallback_download_imports_alongside_the_librarys_copy() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/request/Band - Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="chill")
    task.find_duplicates.return_value = [dup]
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags(REC_URL)):
        _handle_same(plugin, MagicMock(), task)
    task.set_choice.assert_not_called()
    assert task.find_duplicates(None) == []  # hidden from beets' own check
    assert dup._data["sources"] == "chill"
    assert any("[BOTH]" in c.args[0] for c in plugin._log.warning.call_args_list)


def test_a_second_fallback_copy_of_a_recording_is_a_duplicate() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/request/Band - Song.m4a", title="Song")
    task = _task(item=item)
    dup = _dup(via="spotdl", sources="request")
    dup._data["fallback_recording"] = REC
    task.find_duplicates.return_value = [dup]
    with patch("music_scan.music_pipeline._read_spotdl_tags", return_value=SpotdlTags(REC_URL)):
        _handle_same(plugin, MagicMock(), task)
    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


# ---------------------------------------------------------------------------
# clear_singleton_release_track — no recording ID as a release track ID (homelab#2623)
# ---------------------------------------------------------------------------


def test_beets_writes_a_singletons_recording_id_as_its_release_track_id() -> None:
    """The root cause: a singleton match is a bare recording, and beets' TrackInfo maps
    ``release_track_id or track_id`` to mb_releasetrackid (13744, 3809, 17061)."""
    from beets.autotag.hooks import TrackInfo

    item = _file("/x/01 - Song.flac", "Song")
    item.update(TrackInfo(track_id="rec-1", title="Song").item_data)
    assert item.mb_trackid == item.mb_releasetrackid == "rec-1"
    _make_plugin().clear_singleton_release_track(session=MagicMock(), task=_task(item=item))
    assert (item.mb_trackid, item.mb_releasetrackid) == ("rec-1", "")


def test_a_real_release_track_id_and_an_album_task_are_left_alone() -> None:
    plugin = _make_plugin()
    item = _file("/x/01 - Song.flac", "Song", mb_trackid="rec-1", mb_releasetrackid="track-1")
    plugin.clear_singleton_release_track(session=MagicMock(), task=_task(item=item))
    assert item.mb_releasetrackid == "track-1"
    other = _file("/x/02 - Other.flac", "Other", mb_trackid="rec-2", mb_releasetrackid="rec-2")
    plugin.clear_singleton_release_track(session=MagicMock(), task=_task(items=[other]))
    assert other.mb_releasetrackid == "rec-2"


def test_the_release_track_is_cleared_after_the_file_title_is_restored() -> None:
    """Both run at import_task_apply, in this order: a file's own value restore_file_title
    puts back is judged too."""
    from beets.plugins import BeetsPlugin

    calls = []
    with patch.object(BeetsPlugin, "register_listener", lambda self, event, fn: calls.append((event, fn.__name__))):
        with patch.object(BeetsPlugin, "__init__", lambda self, name: None):
            MusicPipelinePlugin()
    applied = [name for event, name in calls if event == "import_task_apply"]
    assert applied == ["restore_file_title", "clear_singleton_release_track"]


# ---------------------------------------------------------------------------
# restore_file_title — a Usenet album track keeps its file's title (homelab#2544)
# ---------------------------------------------------------------------------

LOADED = "/root/Music/inbox/usenet/keep/The Velvet Underground-Loaded-Re-Loaded 45th Anniversary Edition-WEB-FLAC"
DYLAN = "/root/Music/inbox/usenet/keep/Bob Dylan-The Bootleg Series Vol 5 Live 1975-WEB-FLAC"
DURANGO = "Romance in Durango (Live at Harvard Square Theatre, Cambridge, MA - November 1975)"
BABE = "It Ain't Me, Babe (Live at Harvard Square Theatre, Cambridge, MA - November 1975)"


def _file(path: str, title: str, track: int = 1, **tags):
    """A beets Item as read from a release file, before any match."""
    from beets.library import Item

    return Item(path=path, title=title, track=track, artist=tags.pop("artist", "Artist"), **tags)


def _match(item, **info) -> None:
    """What beets' apply_metadata writes from the chosen recording."""
    for field, value in info.items():
        item[field] = value


def _created(plugin, item):
    task = _task(item=item)
    plugin.tag_source_on_created(session=MagicMock(), task=task)
    return task


def _warnings(plugin) -> list[str]:
    return [c.args[0].replace("{}", "%s").replace("{!r}", "%r") % c.args[1:]
            for c in plugin._log.warning.call_args_list]


def test_a_sibling_naming_another_version_gets_the_files_title_and_loses_the_recording() -> None:
    """Loaded's stereo Oh! Sweet Nuthin', matched to the 5.1 mix's recording: only the
    recording names 5.1, so its ID is refuted (homelab#2609; it used to stay for the
    fingerprint recheck, homelab#2540, but retire and the purge trust it first)."""
    plugin = _make_plugin()
    item = _file(f"{LOADED}/10 - Oh! Sweet Nuthin'.flac", "Oh! Sweet Nuthin'", track=10)
    task = _created(plugin, item)
    _match(item, title="Oh! Sweet Nuthin' (5.1 mix)", trackdisambig="5.1 mix",
           mb_trackid="d0063aee", mb_releasetrackid="d0063aee", isrc="USWB10005", work="Oh! Sweet Nuthin'")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert item.title == "Oh! Sweet Nuthin'"
    assert item.trackdisambig == ""
    assert (item.mb_trackid, item.mb_releasetrackid, item.isrc, item.work) == ("", "", "", "")
    assert _warnings(plugin) == [
        "[TITLE] Oh! Sweet Nuthin' (5.1 mix) → Oh! Sweet Nuthin' (recording dropped: another version, 5.1)"]
    assert plugin._file_tags == {}


def test_another_song_gets_the_files_title_and_loses_the_recording() -> None:
    """Dylan's Romance in Durango, matched to It Ain't Me, Babe from the same show."""
    plugin = _make_plugin()
    item = _file(f"{DYLAN}/1-05 - Romance in Durango.flac", DURANGO, track=5, isrc="USSM10500005")
    task = _created(plugin, item)
    _match(item, title=BABE, trackdisambig="live, 1975-11-20", mb_trackid="babe-rec", mb_releasetrackid="babe-rec",
           isrc="USSM17500123", mb_workid="babe-work", work="It Ain't Me, Babe", composers=["Bob Dylan"])

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert item.title == DURANGO
    assert item.trackdisambig == ""
    assert (item.mb_trackid, item.mb_releasetrackid, item.mb_workid, item.work) == ("", "", "", "")
    assert item.composers == []
    assert item.isrc == "USSM10500005"  # the file's own ISRC comes back
    assert _warnings(plugin) == [f"[TITLE] {BABE} → {DURANGO} (recording dropped)"]


def test_a_credit_in_the_files_title_keeps_the_recording() -> None:
    """Ironman's files say Fish feat Raekwon & Cappadonna, MusicBrainz Fish: one song, so
    the file's title goes back and the right recording stays."""
    plugin = _make_plugin()
    item = _file("/root/Music/inbox/usenet/keep/Ghostface-Ironman/05 - Fish.flac",
                 "Fish feat Raekwon & Cappadonna", track=5)
    task = _created(plugin, item)
    _match(item, title="Fish", mb_trackid="fish-rec", mb_releasetrackid="fish-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.title, item.mb_trackid) == ("Fish feat Raekwon & Cappadonna", "fish-rec")
    assert _warnings(plugin) == ["[TITLE] Fish → Fish feat Raekwon & Cappadonna"]


def test_another_song_is_no_duplicate_by_the_wrong_recording() -> None:
    """The duplicate check runs before the restore: the other song's recording and ISRCs
    must not merge the file into, or let a manual import shadow it."""
    plugin = _make_plugin()
    item = _file(f"{DYLAN}/1-05 - Romance in Durango.flac", DURANGO, track=5)
    task = _created(plugin, item)
    task.chosen_info.return_value = {"title": BABE, "track_id": "babe-rec", "isrc": "USSM17500123"}
    manual = _lib_dup(42, via="", mb_trackid="babe-rec", isrc="USSM17500123")
    session = _session({"mb_trackid:babe-rec": [manual], "isrc:USSM17500123": [manual]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()


def test_a_generic_file_title_keeps_beets_title() -> None:
    plugin = _make_plugin()
    item = _file(f"{LOADED}/07.flac", "Track 07", track=7)
    task = _created(plugin, item)
    _match(item, title="Cool It Down", trackdisambig="", mb_trackid="cool-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.title, item.mb_trackid) == ("Cool It Down", "cool-rec")
    assert _warnings(plugin) == ["[TITLE] Cool It Down: the file's title 'Track 07' names nothing; beets' title kept"]


def test_a_case_only_difference_keeps_musicbrainz_spelling() -> None:
    """Heartbreaker's files spell In My Time Of Need, MusicBrainz In My Time of Need:
    no sibling, so beets' title stays and nothing is logged but the cleared disambig."""
    plugin = _make_plugin()
    item = _file(f"{LOADED}/12 - In My Time Of Need.flac", "In My Time Of Need", track=12)
    task = _created(plugin, item)
    _match(item, title="In My Time of Need", trackdisambig="", mb_trackid="need-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.title, item.mb_trackid) == ("In My Time of Need", "need-rec")
    plugin._log.warning.assert_not_called()


def test_a_punctuation_difference_keeps_musicbrainz_spelling_and_finds_the_tags() -> None:
    """Don't against Don’t: beets' title stays, and the pending tags are found by it after the rename."""
    plugin = _make_plugin()
    # A live album: its bare titles don't refute a live recording (homelab#2609).
    item = _file(f"{LOADED}/03 - Don't Ask For The Water.flac", "Don't Ask For The Water", track=3,
                 album="Live at Max's Kansas City")
    task = _created(plugin, item)
    _match(item, title="Don’t Ask for the Water", trackdisambig="live")
    plugin.restore_file_title(session=MagicMock(), task=task)
    item.path = "/root/Music/library/Artist/Album/03 - Don’t Ask for the Water.flac".encode()
    for field in ("sources", "via", "usenet_job"):
        del item[field]

    with patch.object(type(item), "store"):
        plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    assert (item.title, item.trackdisambig) == ("Don’t Ask for the Water", "")
    assert (item.get("sources"), item.get("via")) == ("keep", "usenet")
    assert _warnings(plugin) == ["[TITLE] Don’t Ask for the Water: trackdisambig 'live' cleared"]


def test_a_spacing_difference_keeps_musicbrainz_spelling_and_the_recording() -> None:
    """Illinois' tags drop spaces (Saved theEarth): one song, so the recording stays and
    MusicBrainz's spelling is kept."""
    plugin = _make_plugin()
    item = _file(f"{LOADED}/16 - Temple.flac", "In This Temple as in the Hearts of Man for Whom He Saved theEarth",
                 track=16)
    task = _created(plugin, item)
    _match(item, title="In This Temple as in the Hearts of Man for Whom He Saved the Earth", trackdisambig="",
           mb_trackid="temple-rec", isrc="USA2P0500016")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert item.title == "In This Temple as in the Hearts of Man for Whom He Saved the Earth"
    assert (item.mb_trackid, item.isrc) == ("temple-rec", "USA2P0500016")
    plugin._log.warning.assert_not_called()


def test_a_changed_word_keeps_the_files_title_and_the_recording() -> None:
    """Rock River against MusicBrainz's Rockford River: one song, the file's words win."""
    plugin = _make_plugin()
    file_title = "To the Workers of the Rock River Valley Region, I Have an Idea Concerning Your Predicament"
    item = _file(f"{LOADED}/12 - Workers.flac", file_title, track=12)
    task = _created(plugin, item)
    _match(item, title=file_title.replace("Rock River", "Rockford River"), mb_trackid="workers-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.title, item.mb_trackid) == (file_title, "workers-rec")
    assert "(recording dropped)" not in _warnings(plugin)[0]


NEBRASKA = "/root/Music/inbox/usenet/keep/Bruce.Springsteen-Nebraska.82-EXPANDED.EDITION-24BIT-192KHZ-WEB-FLAC-2025-OBZEN"
STUDIO_RTB = "c7e7f3f2-1edf-424a-a6a2-8d426001f24b"  # Reason to Believe, the 1982 studio recording
LIVE_MFH = "4cb4128a-0e58-4b53-a04f-3551c7292f76"  # My Father's House, live at Red Bank 2025
RED_BANK = "live, 2025‐04‐22: Hackensack Meridian Health Theatre, Red Bank, NJ, USA"


def test_a_live_take_matched_to_the_studio_recording_loses_it() -> None:
    """Nebraska '82 (homelab#2609): beets matched Reason To Believe (Nebraska Live), within
    2 s of the studio track, to the studio recording.  The file names live and the
    recording doesn't: dropped, so the release pass can't retire the live take as held."""
    plugin = _make_plugin()
    item = _file(f"{NEBRASKA}/3-10 - Reason To Believe (Nebraska Live).flac", "Reason To Believe (Nebraska Live)",
                 track=10, album="Nebraska '82: Expanded Edition", isrc="USSM12503413")
    task = _created(plugin, item)
    _match(item, title="Reason to Believe", trackdisambig="version 1b", mb_trackid=STUDIO_RTB,
           mb_releasetrackid=STUDIO_RTB, isrc="USSM18200430")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert item.title == "Reason To Believe (Nebraska Live)"
    assert (item.mb_trackid, item.isrc) == ("", "USSM12503413")
    assert _warnings(plugin) == ["[TITLE] Reason to Believe → Reason To Believe (Nebraska Live) "
                                 "(recording dropped: another version, live)"]


def test_a_studio_track_matched_to_a_live_recording_loses_it() -> None:
    """The reverse (homelab#2609): the Expanded Edition's studio My Father's House, bare,
    matched to the 2025 Red Bank live recording, which only its disambiguation says."""
    plugin = _make_plugin()
    item = _file(f"{NEBRASKA}/4-09 - My Father's House.flac", "My Father's House", track=9,
                 album="Nebraska '82: Expanded Edition")
    task = _created(plugin, item)
    _match(item, title="My Father’s House", trackdisambig=RED_BANK, mb_trackid=LIVE_MFH, isrc="USSM12503412")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.title, item.trackdisambig, item.mb_trackid, item.isrc) == ("My Father’s House", "", "", "")
    assert _warnings(plugin) == [f"[TITLE] My Father’s House: trackdisambig {RED_BANK!r} cleared "
                                 "(recording dropped: another version, live)"]


def test_a_live_albums_bare_title_keeps_its_live_recording() -> None:
    """A live album's tracks are often titled bare: its name (or its release's) covers the
    recording's "live" disambiguation, so the recording stays."""
    plugin = _make_plugin()
    item = _file(f"{DYLAN}/1-05 - Romance in Durango.flac", "Romance in Durango", track=5, album="")
    task = _created(plugin, item)  # the job's name says Live 1975
    _match(item, title="Romance in Durango", trackdisambig="live, 1975-11-20", mb_trackid="durango-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert item.mb_trackid == "durango-rec"


def test_a_live_take_is_no_duplicate_of_the_studio_track_by_the_guessed_recording() -> None:
    """The [BOTH] line (homelab#2609): the duplicate check runs before the restore, and the
    studio recording beets chose for the live take must not make it the studio track's copy."""
    plugin = _make_plugin()
    item = _file(f"{NEBRASKA}/3-10 - Reason To Believe (Nebraska Live).flac", "Reason To Believe (Nebraska Live)",
                 track=10, album="Nebraska '82: Expanded Edition", length=251.5)
    task = _created(plugin, item)
    task.chosen_info.return_value = {"title": "Reason to Believe", "trackdisambig": "version 1b",
                                     "track_id": STUDIO_RTB, "isrc": "USSM18200430"}
    studio = _lib_dup(9045, via="usenet", title="Reason to Believe", mb_trackid=STUDIO_RTB, isrc="USSM18200430",
                      length=250.8)
    session = _session({f"mb_trackid:{STUDIO_RTB}": [studio], "isrc:USSM18200430": [studio]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()
    assert not any("[BOTH]" in line for line in _warnings(plugin))


def test_a_dropped_version_is_a_retitled_event_saying_why(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _file(f"{NEBRASKA}/3-10 - Reason To Believe (Nebraska Live).flac", "Reason To Believe (Nebraska Live)",
                 track=10, album="Nebraska '82: Expanded Edition")
    task = _created(plugin, item)
    _match(item, title="Reason to Believe", trackdisambig="version 1b", mb_trackid=STUDIO_RTB)
    plugin.restore_file_title(session=MagicMock(), task=task)

    _stored(plugin, item, 17052, "/root/Music/library/Bruce Springsteen/Nebraska 82/10 - Reason To Believe.m4a")

    (kind, message, kw), = recorded
    assert kind == "retitled" and kw["item_id"] == 17052
    assert message.endswith("(recording dropped: another version, live)")
    data = kw["data"]
    assert data["recording_dropped"] is True and data["dropped_mb_trackid"] == STUDIO_RTB
    assert (data["dropped_why"], data["versions"]) == ("another version", ["live"])


def test_own_title() -> None:
    from music_scan.music_pipeline import own_title

    assert own_title("Song", 1) == "Song"
    assert own_title("05 - Romance in Durango", 5) == "Romance in Durango"
    assert own_title("5. Song", 5) == "Song"
    assert own_title("7 Seconds", 7) == "7 Seconds"  # no separator: part of the title
    assert own_title("99 Luftballons", 3) == "99 Luftballons"
    for generic in ("", "  ", "05", "Track 5", "track05", "TRACK"):
        assert own_title(generic, 5) is None


def test_a_spotdl_import_keeps_beets_match() -> None:
    plugin = _make_plugin()
    item = _file("/root/Music/inbox/spotdl/chill/The Velvet Underground - Oh! Sweet Nuthin'.m4a", "Oh! Sweet Nuthin'")
    task = _created(plugin, item)
    _match(item, title="Oh! Sweet Nuthin' (2015 remaster)", trackdisambig="2015 remaster", mb_trackid="rem-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.title, item.trackdisambig, item.mb_trackid) == (
        "Oh! Sweet Nuthin' (2015 remaster)", "2015 remaster", "rem-rec")
    plugin._log.warning.assert_not_called()


def test_a_restored_title_finds_the_pending_tags_again() -> None:
    """tag_source_on_stored finds them by the item, whatever beets did to its title and file."""
    plugin = _make_plugin()
    item = _file(f"{LOADED}/10 - 10 - Oh! Sweet Nuthin'.flac", "10 - Oh! Sweet Nuthin'", track=10)
    task = _created(plugin, item)
    _match(item, title="Oh! Sweet Nuthin' (5.1 mix)")
    plugin.restore_file_title(session=MagicMock(), task=task)
    # Renamed by the move, and the flex attrs lost on the way (see tag_source_on_stored).
    item.path = b"/root/Music/library/The Velvet Underground/Loaded/10 - Oh! Sweet Nuthin'.flac"
    for field in ("sources", "via", "usenet_job"):
        del item[field]

    with patch.object(type(item), "store"):
        plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    assert (item.title, item.get("sources"), item.get("via")) == ("Oh! Sweet Nuthin'", "keep", "usenet")
    assert item.get("usenet_job") == "The Velvet Underground-Loaded-Re-Loaded 45th Anniversary Edition-WEB-FLAC"


def test_beets_keeps_the_item_object_from_created_to_apply(monkeypatch, tmp_path) -> None:
    """The restore is keyed by the item object (homelab#2544).  Drive beets' own
    singleton task, candidate lookup and _apply_choice: if an upgrade copies the item
    between import_task_created and import_task_apply, or moves the event after the
    store or before apply_metadata, the library gets beets' title and this fails."""
    from beets import metadata_plugins
    from beets import plugins as beets_plugins
    from beets.autotag.hooks import TrackInfo
    from beets.importer import stages
    from beets.importer.tasks import SingletonImportTask
    from beets.library import Library

    plugin = _make_plugin()
    seen = []

    def on_apply(session, task):
        seen.append((task.item.title, task.item.id))  # beets' title, not stored yet
        plugin.restore_file_title(session=session, task=task)

    listeners = {"import_task_created": [plugin.tag_source_on_created], "import_task_apply": [on_apply]}

    def send(event, **kwargs):
        return [r for f in listeners.get(event, []) if (r := f(**kwargs)) is not None]

    monkeypatch.setattr(beets_plugins, "send", send)
    sibling = TrackInfo(title="Oh! Sweet Nuthin' (5.1 mix)", track_id="d0063aee", trackdisambig="5.1 mix",
                        artist="The Velvet Underground", length=445.0, data_source="MusicBrainz")
    monkeypatch.setattr(metadata_plugins, "item_candidates", lambda *a, **k: iter([sibling]))
    monkeypatch.setattr(metadata_plugins, "tracks_for_ids", lambda *a, **k: iter([]))

    lib = Library(str(tmp_path / "library.db"), str(tmp_path / "library"))
    session = MagicMock(lib=lib)
    item = _file(f"{LOADED}/10 - Oh! Sweet Nuthin'.flac", "Oh! Sweet Nuthin'", track=10,
                 artist="The Velvet Underground", length=445.0)
    task, = SingletonImportTask(None, item).handle_created(session)
    task.lookup_candidates([])
    task.set_choice(task.candidates[0])
    stages._apply_choice(session, task)

    assert seen == [("Oh! Sweet Nuthin' (5.1 mix)", None)]
    stored, = lib.items()
    # Only the recording names 5.1: dropped (homelab#2609).
    assert (stored.title, stored.trackdisambig, stored.mb_trackid) == ("Oh! Sweet Nuthin'", "", "")


def _import_two_equal_titles(monkeypatch, tmp_path, choose, matched=None) -> list[tuple]:
    """Drive two inbox tracks with one title through beets' own singleton tasks, both
    read before either is stored, as the read stage runs ahead of the import
    (homelab#2707).  Returns each stored item's identity."""
    from beets import metadata_plugins
    from beets import plugins as beets_plugins
    from beets.autotag.hooks import TrackInfo
    from beets.importer import stages
    from beets.importer.tasks import SingletonImportTask
    from beets.library import Library

    from music_scan import music_pipeline

    plugin = _make_plugin()
    created = []

    def stored(lib, item):
        assert any(item is c for c in created)  # beets hands item_imported the object it read
        item.path = f"/root/Music/library/{item.artist}/{item.title}.m4a".encode()  # the move
        plugin.tag_source_on_stored(lib=lib, item=item)

    listeners = {"import_task_created": [plugin.tag_source_on_created],
                 "import_task_choice": [plugin.handle_duplicates], "item_imported": [stored]}

    def send(event, **kwargs):
        return [r for f in listeners.get(event, []) if (r := f(**kwargs)) is not None]

    monkeypatch.setattr(beets_plugins, "send", send)
    monkeypatch.setattr(music_pipeline.events, "record", lambda *a, **k: None)
    monkeypatch.setattr(metadata_plugins, "tracks_for_ids", lambda *a, **k: iter([]))
    monkeypatch.setattr(metadata_plugins, "item_candidates", lambda item, artist, title: iter([TrackInfo(
        title=matched or title, artist=artist, track_id=f"mb-{artist}", length=200.0, data_source="MusicBrainz")]))
    tags = {b"/root/Music/inbox/spotdl/111/Artist - Home.m4a": SpotdlTags("https://open.spotify.com/track/AAA", "ISRCAAA"),
            b"/root/Music/inbox/spotdl/222/Other - HOME.m4a": SpotdlTags("https://open.spotify.com/track/BBB", "ISRCBBB")}
    monkeypatch.setattr(music_pipeline, "_read_spotdl_tags", lambda path: tags[path])

    lib = Library(str(tmp_path / "library.db"), str(tmp_path / "library"))
    off = MagicMock()
    off.get.return_value = False
    session = MagicMock(lib=lib, want_resume=False, config={"incremental": False, "copy": off, "delete": off, "move": off})
    tasks = []
    for path, artist, title in ((b"/root/Music/inbox/spotdl/111/Artist - Home.m4a", "Artist", "Home"),
                                (b"/root/Music/inbox/spotdl/222/Other - HOME.m4a", "Other", "HOME")):
        created.append(_file(path, title, artist=artist, length=200.0))
        tasks += SingletonImportTask(None, created[-1]).handle_created(session)
    for task in tasks:
        choose(task)
        stages._apply_choice(session, task)
        task.finalize(session)
    return sorted((i.artist, i.sources, i.spotify_ids, i.spotify_url, i.isrc) for i in lib.items())


TWO_HOMES = [("Artist", "111", "AAA", "https://open.spotify.com/track/AAA", "ISRCAAA"),
             ("Other", "222", "BBB", "https://open.spotify.com/track/BBB", "ISRCBBB")]


def test_two_equal_titles_imported_as_is_keep_their_own_identity(monkeypatch, tmp_path) -> None:
    """The first stored used to take the last read's Spotify ID, ISRC and playlist (homelab#2707)."""
    def choose(task):
        task.set_choice(beets_importer.Action.ASIS)

    assert _import_two_equal_titles(monkeypatch, tmp_path, choose) == TWO_HOMES


def test_two_equal_titles_matched_by_beets_keep_their_own_identity(monkeypatch, tmp_path) -> None:
    def choose(task):
        task.lookup_candidates([])
        task.set_choice(task.candidates[0])

    assert _import_two_equal_titles(monkeypatch, tmp_path, choose) == TWO_HOMES


def test_two_equal_titles_retitled_by_their_match_keep_their_own_identity(monkeypatch, tmp_path) -> None:
    """A title beets changes at import, to one both now share."""
    def choose(task):
        task.lookup_candidates([])
        task.set_choice(task.candidates[0])

    assert _import_two_equal_titles(monkeypatch, tmp_path, choose, matched="Home (2011 Remaster)") == TWO_HOMES


# ---------------------------------------------------------------------------
# imported and retitled events (homelab#2590)
# ---------------------------------------------------------------------------

def _recorded(monkeypatch) -> list:
    from music_scan import music_pipeline

    recorded = []
    monkeypatch.setattr(music_pipeline.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    return recorded


def _stored(plugin, item, item_id: int, path: str) -> None:
    """item_imported: beets stored the item (it has an ID) and moved its file."""
    item.id = item_id
    item.path = path.encode()
    with patch.object(type(item), "store"):
        plugin.tag_source_on_stored(lib=MagicMock(), item=item)


def test_a_title_put_back_is_a_retitled_event_on_the_stored_item(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _file(f"{DYLAN}/1-05 - Romance in Durango.flac", DURANGO, track=5)
    task = _created(plugin, item)
    _match(item, title=BABE, trackdisambig="live, 1975-11-20", mb_trackid="babe-rec")
    plugin.restore_file_title(session=MagicMock(), task=task)
    assert recorded == []  # no item ID yet: the event waits for item_imported

    _stored(plugin, item, 77, "/root/Music/library/Bob Dylan/Live 1975/1-05 - Romance in Durango.flac")

    (kind, message, kw), = recorded
    assert kind == "retitled" and kw["item_id"] == 77 and kw["source"] == "import"
    assert message == f"[TITLE] {BABE} → {DURANGO} (recording dropped)"
    data = kw["data"]
    assert (data["matched"], data["title"], data["trackdisambig"]) == (BABE, DURANGO, "live, 1975-11-20")
    assert data["recording_dropped"] is True and data["dropped_mb_trackid"] == "babe-rec"
    assert (data["dropped_why"], data["versions"]) == ("another song", None)
    assert data["usenet_job"] == DYLAN.rsplit("/", 1)[1]
    assert plugin._retitled == {}


def test_a_cleared_trackdisambig_alone_is_a_retitled_event(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _file(f"{LOADED}/03 - Don't Ask For The Water.flac", "Don't Ask For The Water", track=3,
                 album="Live at Max's Kansas City")
    task = _created(plugin, item)
    _match(item, title="Don’t Ask for the Water", trackdisambig="live", mb_trackid="water-rec")
    plugin.restore_file_title(session=MagicMock(), task=task)

    _stored(plugin, item, 78, "/root/Music/library/Artist/Album/03 - Don’t Ask for the Water.flac")

    retitled = [(m, kw) for kind, m, kw in recorded if kind == "retitled"]
    assert retitled == [("[TITLE] Don’t Ask for the Water: trackdisambig 'live' cleared", retitled[0][1])]
    assert retitled[0][1]["data"]["recording_dropped"] is False
    assert retitled[0][1]["data"]["dropped_mb_trackid"] is None
    # A Usenet item is one of a release: its album's [DONE] stands for the import.
    assert [kind for kind, _, _ in recorded] == ["retitled"]


def test_a_title_left_as_matched_records_nothing(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _file(f"{LOADED}/12 - In My Time Of Need.flac", "In My Time Of Need", track=12)
    task = _created(plugin, item)
    _match(item, title="In My Time of Need", trackdisambig="", mb_trackid="need-rec")
    plugin.restore_file_title(session=MagicMock(), task=task)

    _stored(plugin, item, 79, "/root/Music/library/Artist/Album/12 - In My Time of Need.m4a")

    assert recorded == [] and plugin._retitled == {}


def test_a_spotdl_import_is_an_imported_event(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/03 - My Track.m4a", title="My Track")
    _pend(plugin, item, "jazz", SpotdlTags(URL_X, "GBAAA001001"))
    item.id = 42

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    (kind, message, kw), = recorded
    assert kind == "imported" and kw["item_id"] == 42 and kw["source"] == "import"
    assert message.startswith("[IMPORTED] jazz: ")
    assert kw["data"]["playlist"] == "jazz" and kw["data"]["spotify_ids"] == ["X"]
    assert kw["data"]["fallback_recording"] is None


def test_a_request_fallback_import_says_its_recording_was_verified(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _item("/root/Music/library/Band/Album/01 - Song.m4a", title="Song")
    _pend(plugin, item, "request", SpotdlTags(REC_URL, None))

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    (kind, message, kw), = recorded
    assert kind == "imported" and message.endswith(f"(fallback, recording {REC} verified)")
    assert kw["data"]["fallback_recording"] == REC and kw["data"]["mb_trackid"] == REC


def test_an_item_from_no_playlist_records_nothing(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a", title="Track")

    plugin.tag_source_on_stored(lib=MagicMock(), item=item)

    assert recorded == []


# ---------------------------------------------------------------------------
# import_alongside and kept_apart events (homelab#2590)
# ---------------------------------------------------------------------------


def _imported(plugin, item) -> None:
    """item_imported, for a mock item: beets stored it and moved its file."""
    item.id = 88
    item.path = b"/root/Music/library/Song.m4a"
    item["path"] = item.path
    plugin.tag_source_on_stored(lib=MagicMock(), item=item)


def test_a_split_is_a_kept_apart_event_on_the_stored_item(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBLIVE"))
    studio = _lib_dup(7, isrc="GBSTUDIO")
    task.find_duplicates.return_value = [studio]

    plugin.handle_duplicates(session=_session({}), task=task)
    assert recorded == []  # no item ID yet: the event waits for item_imported

    _imported(plugin, item)

    [(kind, message, kw)] = [r for r in recorded if r[0] != "imported"]
    assert kind == "kept_apart" and kw["item_id"] == 88 and kw["source"] == "import"
    assert message.startswith("[SPLIT] ") and "(ISRCs GBLIVE vs GBSTUDIO)" in message
    data = kw["data"]
    assert (data["why"], data["isrcs"]) == ("ISRCs differ", ["GBLIVE"])
    assert (data["duplicate"]["item_id"], data["duplicate"]["isrc"]) == (7, "GBSTUDIO")
    assert data["path"] == "/root/Music/library/Song.m4a"
    assert plugin._beside == {}


def test_no_shared_id_is_a_kept_apart_event(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin, item, task = _incoming(SpotdlTags(URL_X), title="Girlfriend Is Better")
    task.find_duplicates.return_value = [_lib_dup(2530, isrc="USWB19900873")]

    plugin.handle_duplicates(session=_session({}), task=task)
    _imported(plugin, item)

    [(kind, message, kw)] = [r for r in recorded if r[0] != "imported"]
    assert kind == "kept_apart" and message.startswith("[APART] ") and kw["data"]["why"] == "no shared ID"
    assert "isrcs" not in kw["data"]


def _usenet_incoming() -> tuple:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/usenet/album/a/Song.m4a", title="Song")
    _pend(plugin, item, "album", via="usenet")
    task = _task(item=item)
    task.chosen_info.return_value = {"track_id": "mb-rec-1", "isrc": None}
    return plugin, item, task


def test_each_duplicate_of_a_usenet_track_is_its_own_import_alongside_event(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin, item, task = _usenet_incoming()
    released = _lib_dup(7, via="usenet", mb_trackid="mb-rec-1", isrc="GBA")
    loose = _lib_dup(8, via="spotdl", mb_trackid="mb-rec-1")

    plugin.handle_duplicates(session=_session({"mb_trackid:mb-rec-1": [released, loose]}), task=task)
    _imported(plugin, item)

    alongside = [(m, kw) for kind, m, kw in recorded if kind == "import_alongside"]
    assert [(m.split(" ")[0], kw["data"]["why"], kw["data"]["duplicate"]["item_id"], kw["item_id"])
            for m, kw in alongside] == [("[BOTH]", "another release's copy", 7, 88),
                                        ("[ALBUM]", "the album copy supersedes it", 8, 88)]
    assert alongside[0][1]["data"]["duplicate"]["via"] == "usenet"


def test_an_album_requests_copy_is_an_import_alongside_event(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin, item, task = _incoming(SpotdlTags(REC_URL))
    task.find_duplicates.return_value = [_lib_dup(7, sources="chill")]

    _handle_same(plugin, MagicMock(), task)
    _imported(plugin, item)

    [(message, kw)] = [(m, kw) for kind, m, kw in recorded if kind == "import_alongside"]
    assert message.startswith("[BOTH] ") and kw["data"]["why"] == "the album request's copy"


def test_an_import_that_never_lands_records_nothing_beside(monkeypatch) -> None:
    recorded = _recorded(monkeypatch)
    plugin, item, task = _incoming(SpotdlTags(URL_X, "GBLIVE"))
    task.find_duplicates.return_value = [_lib_dup(7, isrc="GBSTUDIO")]

    plugin.handle_duplicates(session=_session({}), task=task)
    plugin._record_beside(_item("/root/Music/inbox/spotdl/playlist-b/Other.m4a", title="Other"))

    assert recorded == [] and list(plugin._beside) == [id(item)]


# ---------------------------------------------------------------------------
# restore_file_title — a match by another artist is refuted (homelab#2649)
# ---------------------------------------------------------------------------

BORN = "/root/Music/inbox/usenet/cool/Springsteen Bruce - 1984 Born In The U.S.A no scans"
VOLTS = {"artist": "5000 Volts", "artist_sort": "5000 Volts", "artist_credit": "5000 Volts", "artists": ["5000 Volts"],
         "artists_sort": ["5000 Volts"], "artists_credit": ["5000 Volts"], "mb_artistid": "16dcd746",
         "mb_artistids": ["16dcd746"], "mb_trackid": "113bb21e", "mb_releasetrackid": "113bb21e",
         "isrc": "GBF087500013", "arrangers": ["Tony Eyers"], "title": "I'm on Fire"}


def _on_fire(plugin, **tags):
    tags.setdefault("artist", "Bruce Springsteen")
    item = _file(f"{BORN}/06 - I'm on Fire.m4a", "I'm on Fire", track=6, **tags)
    return item, _created(plugin, item)


def test_another_artists_song_of_the_title_loses_the_recording_and_the_file_keeps_its_artist() -> None:
    """18518: Springsteen's I'm on Fire took a 5000 Volts recording, with their artist tags."""
    plugin = _make_plugin()
    item, task = _on_fire(plugin, albumartist="Bruce Springsteen")
    _match(item, **VOLTS)

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.mb_trackid, item.mb_releasetrackid, item.isrc, item.arrangers) == ("", "", "", [])
    assert item.artist == "Bruce Springsteen"
    assert (item.artist_sort, item.artist_credit, item.mb_artistid) == ("", "", "")
    assert (item.artists, item.artists_sort, item.artists_credit, item.mb_artistids) == ([], [], [], [])
    assert item.albumartist == "Bruce Springsteen"
    assert _warnings(plugin) == ["[TITLE] I'm on Fire (recording dropped: another artist, 5000 Volts)"]


def test_a_refuted_artist_is_a_retitled_event(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr("music_scan.music_pipeline.events.record", lambda *a, **k: recorded.append((a, k)))
    plugin = _make_plugin()
    item, task = _on_fire(plugin)
    _match(item, **VOLTS)
    plugin.restore_file_title(session=MagicMock(), task=task)

    plugin._record_retitle(item)

    (kind, message), kwargs = recorded[0]
    assert kind == "retitled" and "another artist, 5000 Volts" in message
    data = kwargs["data"]
    assert data["recording_dropped"] and data["dropped_why"] == "another artist"
    assert data["dropped_mb_trackid"] == "113bb21e"
    assert (data["file_artist"], data["matched_artist"]) == ("Bruce Springsteen", "5000 Volts")
    assert data["artist"] == "Bruce Springsteen"


def test_another_song_by_another_artist_puts_the_artist_back_too() -> None:
    plugin = _make_plugin()
    item = _file(f"{DYLAN}/1-05 - Romance in Durango.flac", DURANGO, track=5, artist="Bob Dylan")
    task = _created(plugin, item)
    _match(item, title=BABE, artist="Joan Baez", mb_trackid="babe-rec")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.artist, item.mb_trackid) == ("Bob Dylan", "")
    assert _warnings(plugin) == [f"[TITLE] {BABE} → {DURANGO} (recording dropped)"]


def _kept(plugin, item, task, artist: str, **match) -> None:
    _match(item, artist=artist, mb_trackid="rec", title="I'm on Fire", **match)
    plugin.restore_file_title(session=MagicMock(), task=task)
    assert (item.artist, item.mb_trackid) == (artist, "rec")
    assert _warnings(plugin) == []


def test_a_band_credit_keeps_the_match() -> None:
    """18513 and its like: the file says Bruce Springsteen, the recording adds the band."""
    plugin = _make_plugin()
    item, task = _on_fire(plugin)
    _kept(plugin, item, task, "Bruce Springsteen & The E Street Band")


def test_a_featured_artist_keeps_the_match() -> None:
    plugin = _make_plugin()
    item, task = _on_fire(plugin, artist="Bruce Springsteen feat. Clarence Clemons")
    _kept(plugin, item, task, "Bruce Springsteen")


def test_a_guest_on_the_album_artists_album_keeps_the_match() -> None:
    plugin = _make_plugin()
    item, task = _on_fire(plugin, artist="Clarence Clemons", albumartist="Bruce Springsteen")
    _kept(plugin, item, task, "Bruce Springsteen")


def test_a_compilations_file_keeps_the_match() -> None:
    for tags in ({"comp": True}, {"albumartist": "Various Artists"}, {"albumtypes": ["compilation"]},
                 {"artist": "Various Artists"}):
        plugin = _make_plugin()
        item, task = _on_fire(plugin, **tags)
        _kept(plugin, item, task, "5000 Volts")


def test_a_file_with_no_artist_tag_keeps_the_match() -> None:
    plugin = _make_plugin()
    item, task = _on_fire(plugin, artist="", albumartist="Bruce Springsteen")
    _kept(plugin, item, task, "5000 Volts")


def test_another_script_keeps_the_match() -> None:
    plugin = _make_plugin()
    item, task = _on_fire(plugin, artist="坂本龍一")
    _kept(plugin, item, task, "Ryuichi Sakamoto")


def test_a_credited_name_keeps_the_match() -> None:
    """MusicBrainz's artist is the canonical name, its credit what the release printed."""
    plugin = _make_plugin()
    item, task = _on_fire(plugin, artist="The Artist")
    _kept(plugin, item, task, "Prince", artist_credit="The Artist")


def test_the_same_musicbrainz_artist_keeps_the_match() -> None:
    plugin = _make_plugin()
    item, task = _on_fire(plugin, artist="Yusuf", mb_artistid="same")
    _kept(plugin, item, task, "Cat Stevens", mb_artistids=["same"])


def test_a_generic_file_title_keeps_the_match_whatever_its_artist() -> None:
    """The title rule's scope: a file title naming nothing leaves beets' match alone."""
    plugin = _make_plugin()
    item = _file(f"{BORN}/track06.m4a", "Track 06", track=6, artist="Bruce Springsteen")
    task = _created(plugin, item)
    _match(item, artist="5000 Volts", mb_trackid="rec", title="I'm on Fire")

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.artist, item.mb_trackid, item.title) == ("5000 Volts", "rec", "I'm on Fire")


def test_a_spotdl_import_keeps_a_match_by_another_artist() -> None:
    """Only Usenet files are captured: spotdl imports keep beets' match (homelab#2544)."""
    plugin = _make_plugin()
    item = _file("/root/Music/inbox/spotdl/keep/Bruce Springsteen - I'm on Fire.m4a", "I'm on Fire",
                 artist="Bruce Springsteen")
    task = _created(plugin, item)
    _match(item, **VOLTS)

    plugin.restore_file_title(session=MagicMock(), task=task)

    assert (item.artist, item.mb_trackid) == ("5000 Volts", "113bb21e")
    assert _warnings(plugin) == []


def test_another_artists_recording_is_no_duplicate() -> None:
    """The duplicate check runs before the restore, on the match's info: 5000 Volts' own
    copy in the library must not take the file in or skip it."""
    plugin = _make_plugin()
    item, task = _on_fire(plugin)
    task.chosen_info.return_value = {"title": "I'm on Fire", "artist": "5000 Volts", "artist_credit": "5000 Volts",
                                     "artists": ["5000 Volts"], "artist_id": "16dcd746", "track_id": "113bb21e",
                                     "isrc": "GBF087500013"}
    manual = _lib_dup(42, via="", mb_trackid="113bb21e", isrc="GBF087500013")
    session = _session({"mb_trackid:113bb21e": [manual], "isrc:GBF087500013": [manual]})

    plugin.handle_duplicates(session=session, task=task)

    task.set_choice.assert_not_called()

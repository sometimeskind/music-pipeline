"""Tests for pipeline.music_pipeline — pure helpers and listener methods."""

from unittest.mock import MagicMock, patch

from beets import importer as beets_importer

from pipeline.music_pipeline import (
    MusicPipelinePlugin,
    _all_via_spotdl,
    _playlist_from_path,
)


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
    return plugin


def _item(path: str, source: str = "", via: str = "", title: str = "") -> MagicMock:
    """Mock beets Item with a path and readable/writable flexible attributes."""
    m = MagicMock()
    m.path = path
    m.title = title
    data = {"source": source, "via": via}
    m.get = lambda k, default="": data.get(k, default)
    # __setitem__ tracked by MagicMock; also update data so .get() sees writes.
    def _setitem(k, v):
        data[k] = v
    m.__setitem__ = MagicMock(side_effect=_setitem)
    return m


def _dup(via: str = "spotdl") -> MagicMock:
    """Mock a library Item used as a duplicate (not a beets_library.Album)."""
    d = MagicMock(spec=[])  # spec=[] prevents hasattr from matching Album
    data = {"via": via}
    d.get = lambda k, default="": data.get(k, default)
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

    item.__setitem__.assert_any_call("source", "jazz")
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
        item.__setitem__.assert_any_call("source", "pop")
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

    item.__setitem__.assert_any_call("source", "jazz")
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

    item.__setitem__.assert_any_call("source", "jazz")
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

    item.__setitem__.assert_any_call("source", "rock")
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


def test_handle_duplicates_spotdl_only_defers_to_config() -> None:
    """All-spotdl duplicates: plugin steps aside; beets config handles removal."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="spotdl")]

    plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_not_called()


def test_handle_duplicates_manual_duplicate_sets_skip() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]  # no via = manual

    with patch("pipeline.music_pipeline.Path"):
        plugin.handle_duplicates(session=MagicMock(), task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_handle_duplicates_manual_duplicate_deletes_inbox_file() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]

    with patch("pipeline.music_pipeline.Path") as mock_path:
        plugin.handle_duplicates(session=MagicMock(), task=task)

    mock_path.return_value.unlink.assert_called_once_with(missing_ok=True)

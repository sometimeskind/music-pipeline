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
    return plugin


def _item(path: str, source: str = "", via: str = "") -> MagicMock:
    """Mock beets Item with a path and readable/writable flexible attributes."""
    m = MagicMock()
    m.path = path
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
# MusicPipelinePlugin.tag_source — tagging
# ---------------------------------------------------------------------------

def test_tag_source_singleton_in_inbox() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = []

    plugin.tag_source(session=MagicMock(), task=task)

    item.__setitem__.assert_any_call("source", "jazz")
    item.__setitem__.assert_any_call("via", "spotdl")


def test_tag_source_singleton_outside_inbox() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a")
    task = _task(item=item)

    plugin.tag_source(session=MagicMock(), task=task)

    item.__setitem__.assert_not_called()


def test_tag_source_album_task_tags_all_items() -> None:
    plugin = _make_plugin()
    item1 = _item("/root/Music/inbox/spotdl/pop/a.m4a")
    item2 = _item("/root/Music/inbox/spotdl/pop/b.m4a")
    task = _task(items=[item1, item2])
    task.find_duplicates.return_value = []

    plugin.tag_source(session=MagicMock(), task=task)

    for item in (item1, item2):
        item.__setitem__.assert_any_call("source", "pop")
        item.__setitem__.assert_any_call("via", "spotdl")


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source — duplicate handling
# ---------------------------------------------------------------------------

def test_tag_source_skip_choice_skips_duplicate_check() -> None:
    """Tasks already marked SKIP should not trigger a duplicate check."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(choice_flag=beets_importer.Action.SKIP, item=item)

    plugin.tag_source(session=MagicMock(), task=task)

    task.find_duplicates.assert_not_called()


def test_tag_source_no_duplicates_does_not_skip() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = []

    plugin.tag_source(session=MagicMock(), task=task)

    task.set_choice.assert_not_called()


def test_tag_source_spotdl_only_duplicates_defers_to_config() -> None:
    """All-spotdl duplicates: plugin steps aside; beets config handles removal."""
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="spotdl")]

    plugin.tag_source(session=MagicMock(), task=task)

    task.set_choice.assert_not_called()


def test_tag_source_manual_duplicate_sets_skip() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]  # no via = manual

    with patch("pipeline.music_pipeline.Path"):
        plugin.tag_source(session=MagicMock(), task=task)

    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)


def test_tag_source_manual_duplicate_deletes_inbox_file() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = _task(item=item)
    task.find_duplicates.return_value = [_dup(via="")]

    with patch("pipeline.music_pipeline.Path") as mock_path:
        plugin.tag_source(session=MagicMock(), task=task)

    mock_path.return_value.unlink.assert_called_once_with(missing_ok=True)

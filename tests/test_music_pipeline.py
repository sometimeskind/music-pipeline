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
    # __setitem__ is automatically tracked by MagicMock; also update data so
    # subsequent .get() calls see the written value.
    def _setitem(k, v):
        data[k] = v
    m.__setitem__ = MagicMock(side_effect=_setitem)
    return m


def _dup(source: str = "", via: str = "") -> MagicMock:
    """Mock an existing library item used as a duplicate."""
    d = MagicMock()
    data = {"source": source, "via": via}
    d.get = lambda k, default="": data.get(k, default)
    d.__getitem__ = lambda self, k: data[k]
    return d


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
    assert _all_via_spotdl([]) is True  # vacuously true — guarded by `if not duplicates` in caller

def test_all_via_spotdl_none_via() -> None:
    assert _all_via_spotdl([_via_item("")]) is False


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.tag_source
# ---------------------------------------------------------------------------

def test_tag_source_singleton_in_inbox() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")
    task = MagicMock()
    task.item = item

    plugin.tag_source(session=MagicMock(), task=task)

    item.__setitem__.assert_any_call("source", "jazz")
    item.__setitem__.assert_any_call("via", "spotdl")


def test_tag_source_singleton_outside_inbox() -> None:
    plugin = _make_plugin()
    item = _item("/root/Music/library/Artist/Album/track.m4a")
    task = MagicMock()
    task.item = item

    plugin.tag_source(session=MagicMock(), task=task)

    item.__setitem__.assert_not_called()


def test_tag_source_album_task_tags_all_items() -> None:
    plugin = _make_plugin()
    item1 = _item("/root/Music/inbox/spotdl/pop/a.m4a")
    item2 = _item("/root/Music/inbox/spotdl/pop/b.m4a")
    task = MagicMock()
    task.item = None
    task.items = [item1, item2]

    plugin.tag_source(session=MagicMock(), task=task)

    for item in (item1, item2):
        item.__setitem__.assert_any_call("source", "pop")
        item.__setitem__.assert_any_call("via", "spotdl")


# ---------------------------------------------------------------------------
# MusicPipelinePlugin.duplicate_action
# ---------------------------------------------------------------------------

def test_duplicate_action_empty_list_returns_none() -> None:
    plugin = _make_plugin()
    task = MagicMock()
    task.item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")

    assert plugin.duplicate_action(MagicMock(), task, []) is None


def test_duplicate_action_all_spotdl_returns_remove() -> None:
    plugin = _make_plugin()
    task = MagicMock()
    task.item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")

    result = plugin.duplicate_action(MagicMock(), task, [_dup(source="jazz", via="spotdl")])

    assert result is beets_importer.action.REMOVE


def test_duplicate_action_all_spotdl_inherits_source() -> None:
    plugin = _make_plugin()
    incoming = _item("/root/Music/inbox/spotdl/jazz/track.m4a", source="")
    task = MagicMock()
    task.item = incoming

    plugin.duplicate_action(MagicMock(), task, [_dup(source="jazz", via="spotdl")])

    incoming.__setitem__.assert_any_call("source", "jazz")


def test_duplicate_action_manual_dup_returns_skip() -> None:
    plugin = _make_plugin()
    task = MagicMock()
    task.item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")

    with patch("pipeline.music_pipeline.Path"):
        result = plugin.duplicate_action(MagicMock(), task, [_dup(via="")])

    assert result is beets_importer.action.SKIP


def test_duplicate_action_manual_dup_deletes_inbox_file() -> None:
    plugin = _make_plugin()
    task = MagicMock()
    task.item = _item("/root/Music/inbox/spotdl/jazz/track.m4a")

    with patch("pipeline.music_pipeline.Path") as mock_path:
        plugin.duplicate_action(MagicMock(), task, [_dup(via="")])

    mock_path.return_value.unlink.assert_called_once_with(missing_ok=True)

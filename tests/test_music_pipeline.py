"""Tests for pipeline.music_pipeline — pure helper logic."""

from unittest.mock import MagicMock

from pipeline.music_pipeline import _playlist_from_path, _all_via_spotdl


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

def _item(via: str) -> MagicMock:
    m = MagicMock()
    m.get = lambda key, default="": {"via": via}.get(key, default)
    return m

def test_all_via_spotdl_all_spotdl() -> None:
    assert _all_via_spotdl([_item("spotdl"), _item("spotdl")]) is True

def test_all_via_spotdl_one_manual() -> None:
    assert _all_via_spotdl([_item("spotdl"), _item("")]) is False

def test_all_via_spotdl_empty() -> None:
    assert _all_via_spotdl([]) is True  # vacuously true — no duplicates to guard against

def test_all_via_spotdl_none_via() -> None:
    assert _all_via_spotdl([_item("")]) is False

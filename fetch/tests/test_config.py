"""Tests for pipeline.config — playlist config parsing."""

import textwrap
from pathlib import Path

import pytest

from pipeline.config import PlaylistConfig, load_playlists


def write_conf(tmp_path: Path, content: str) -> Path:
    conf = tmp_path / "playlists.conf"
    conf.write_text(textwrap.dedent(content), encoding="utf-8")
    return conf


def test_basic_entries(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        liked-songs   https://open.spotify.com/playlist/AAA
        workout-mix   https://open.spotify.com/playlist/BBB
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 2
    assert playlists[0] == PlaylistConfig("liked-songs", "https://open.spotify.com/playlist/AAA", nosync=False)
    assert playlists[1] == PlaylistConfig("workout-mix", "https://open.spotify.com/playlist/BBB", nosync=False)


def test_nosync_flag(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        static-pl  https://open.spotify.com/playlist/CCC  nosync
        live-pl    https://open.spotify.com/playlist/DDD
        """,
    )
    playlists = load_playlists(conf)
    assert playlists[0].nosync is True
    assert playlists[1].nosync is False


def test_comments_and_blank_lines(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        # This is a comment
        my-pl  https://open.spotify.com/playlist/EEE

        # Another comment
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 1
    assert playlists[0].name == "my-pl"


def test_inline_comment(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        "my-pl  https://open.spotify.com/playlist/FFF  # some comment\n",
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 1
    assert playlists[0].name == "my-pl"
    assert playlists[0].nosync is False


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        good-pl  https://open.spotify.com/playlist/GGG
        bad-only-one-token
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 1
    assert playlists[0].name == "good-pl"


def test_empty_file(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "")
    assert load_playlists(conf) == []

"""Tests for pipeline.config — playlist config parsing."""

import textwrap
from pathlib import Path

import pytest

from music_fetch.config import PlaylistConfig, load_playlists


def write_conf(tmp_path: Path, content: str) -> Path:
    conf = tmp_path / "playlists.conf"
    conf.write_text(textwrap.dedent(content), encoding="utf-8")
    return conf


def test_basic_entries(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        liked-songs   https://open.spotify.com/playlist/AAA0000000000000000000
        workout-mix   https://open.spotify.com/playlist/BBB0000000000000000000
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 2
    assert playlists[0] == PlaylistConfig("liked-songs", "https://open.spotify.com/playlist/AAA0000000000000000000", nosync=False)
    assert playlists[1] == PlaylistConfig("workout-mix", "https://open.spotify.com/playlist/BBB0000000000000000000", nosync=False)


def test_nosync_flag(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        static-pl  https://open.spotify.com/playlist/CCC0000000000000000000  nosync
        live-pl    https://open.spotify.com/playlist/DDD0000000000000000000
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
        my-pl  https://open.spotify.com/playlist/EEE0000000000000000000

        # Another comment
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 1
    assert playlists[0].name == "my-pl"


def test_inline_comment(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        "my-pl  https://open.spotify.com/playlist/FFF0000000000000000000  # some comment\n",
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 1
    assert playlists[0].name == "my-pl"
    assert playlists[0].nosync is False


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        good-pl  https://open.spotify.com/playlist/GGG0000000000000000000
        bad-only-one-token
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 1
    assert playlists[0].name == "good-pl"


def test_empty_file(tmp_path: Path) -> None:
    conf = write_conf(tmp_path, "")
    assert load_playlists(conf) == []


def test_clash_raises_for_substring(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        rock       https://open.spotify.com/playlist/AAA0000000000000000000
        hard-rock  https://open.spotify.com/playlist/BBB0000000000000000000
        """,
    )
    with pytest.raises(ValueError, match="clash"):
        load_playlists(conf)


def test_clash_raises_for_prefix(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        pop       https://open.spotify.com/playlist/AAA0000000000000000000
        pop-hits  https://open.spotify.com/playlist/BBB0000000000000000000
        """,
    )
    with pytest.raises(ValueError, match="clash"):
        load_playlists(conf)


def test_clash_raises_for_suffix(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        hits      https://open.spotify.com/playlist/AAA0000000000000000000
        pop-hits  https://open.spotify.com/playlist/BBB0000000000000000000
        """,
    )
    with pytest.raises(ValueError, match="clash"):
        load_playlists(conf)


def test_clash_raises_for_exact_duplicate(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        rock  https://open.spotify.com/playlist/AAA0000000000000000000
        rock  https://open.spotify.com/playlist/BBB0000000000000000000
        """,
    )
    with pytest.raises(ValueError, match="clash"):
        load_playlists(conf)


def test_no_clash_for_distinct_names(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        rock-classics  https://open.spotify.com/playlist/AAA0000000000000000000
        pop-hits       https://open.spotify.com/playlist/BBB0000000000000000000
        """,
    )
    playlists = load_playlists(conf)
    assert len(playlists) == 2


def test_album_flag(tmp_path: Path) -> None:
    conf = write_conf(
        tmp_path,
        """
        later   https://open.spotify.com/playlist/AAA0000000000000000000  album
        keep    https://open.spotify.com/playlist/BBB0000000000000000000
        frozen  https://open.spotify.com/playlist/CCC0000000000000000000  nosync
        """,
    )
    later, keep, frozen = load_playlists(conf)
    assert later.album and not later.nosync
    assert not keep.album
    assert frozen.nosync and not frozen.album


def test_ids_and_urls_load_as_canonical_urls(tmp_path: Path) -> None:
    """homelab#2411: a bare ID is a playlist's; any URL spelling reduces to one key."""
    pl = "37i9dQZF1DXcBWIGoYBM5M"
    album = "4aawyAB9vmqN3uQ7FjRGTy"
    conf = write_conf(
        tmp_path,
        f"""
        bare    {pl}
        intl    https://open.spotify.com/intl-de/playlist/{pl}?si=0123abcd
        record  https://open.spotify.com/album/{album}?si=x
        """,
    )
    assert [p.url for p in load_playlists(conf)] == [
        f"https://open.spotify.com/playlist/{pl}",
        f"https://open.spotify.com/playlist/{pl}",
        f"https://open.spotify.com/album/{album}",
    ]


@pytest.mark.parametrize("ref", [
    "abc",
    "https://open.spotify.com/track/37i9dQZF1DXcBWIGoYBM5M",
    "https://example.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
])
def test_bad_spotify_reference_raises(tmp_path: Path, ref: str) -> None:
    conf = write_conf(tmp_path, f"pl  {ref}\n")
    with pytest.raises(ValueError, match="not a Spotify"):
        load_playlists(conf)

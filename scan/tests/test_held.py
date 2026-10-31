"""Held downloads (homelab#1914): spotdl URLs in quarantine or waiting in the
inbox, which the nightly never fetches again."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from music_scan import held


def _files(root: Path, urls: dict[str, str | None]) -> dict[Path, str | None]:
    """Create empty files under *root*; returns {path: the WOAS URL it 'carries'}."""
    out = {}
    for rel, url in urls.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        out[p] = url
    return out


def test_spotify_urls_reads_every_m4a_under_the_directory(tmp_path: Path) -> None:
    tags = _files(tmp_path, {
        "spotdl/later/a.m4a": "https://open.spotify.com/track/A",
        "rejected/spotdl/later/b.m4a": "https://open.spotify.com/track/B",
        "c.m4a": None,  # no WOAS tag: not a spotdl download
        "usenet/job/d.flac": "https://open.spotify.com/track/D",  # only .m4a is read
    })
    with mock.patch.object(held, "_read_spotify_url", side_effect=lambda p: tags[p]) as read:
        assert held.spotify_urls(tmp_path) == {"https://open.spotify.com/track/A", "https://open.spotify.com/track/B"}
    assert {c.args[0].name for c in read.call_args_list} == {"a.m4a", "b.m4a", "c.m4a"}


def test_spotify_urls_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert held.spotify_urls(tmp_path / "missing") == frozenset()


def test_held_urls_are_quarantine_and_inbox(tmp_path: Path) -> None:
    quarantine, inbox = tmp_path / "quarantine", tmp_path / "inbox" / "spotdl"
    tags = {
        **_files(quarantine, {"spotdl/pl/q.m4a": "https://open.spotify.com/track/Q"}),
        **_files(inbox, {"pl/i.m4a": "https://open.spotify.com/track/I",
                         "pl/dup.m4a": "https://open.spotify.com/track/Q"}),
    }
    with mock.patch.object(held, "_read_spotify_url", side_effect=lambda p: tags[p]):
        urls = held.held_urls(quarantine, inbox)
    assert urls == {"https://open.spotify.com/track/Q", "https://open.spotify.com/track/I"}
    assert isinstance(urls, frozenset)


def test_read_spotify_url_of_an_unreadable_file_is_none(tmp_path: Path) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not an mp4")
    assert held._read_spotify_url(junk) is None
    assert held._read_spotify_url(tmp_path / "missing.m4a") is None


def test_held_urls_read_real_spotdl_tags(tmp_path: Path, clip) -> None:
    """End to end through mutagen: a spotdl m4a's ``----:spotdl:WOAS`` URL."""
    quarantine, inbox = tmp_path / "quarantine", tmp_path / "inbox"
    (quarantine / "rejected").mkdir(parents=True)
    (inbox / "pl").mkdir(parents=True)
    clip(quarantine / "rejected" / "q.m4a", ("tone", 1), url="https://open.spotify.com/track/Q")
    clip(inbox / "pl" / "i.m4a", ("tone", 1), url="https://open.spotify.com/track/I")
    clip(inbox / "pl" / "untagged.m4a", ("tone", 1), url=None)
    assert held._read_spotify_url(inbox / "pl" / "untagged.m4a") is None
    assert held.held_urls(quarantine, inbox) == {"https://open.spotify.com/track/Q", "https://open.spotify.com/track/I"}


def test_album_held_is_the_wants_records_tracks_only_when_on(tmp_path: Path, monkeypatch) -> None:
    """#324: the tracks album mode is about to grab for a spotdl playlist."""
    from music_fetch import albums

    from music_fetch import state as statedb

    monkeypatch.setenv("ALBUM_MODE", "on")
    assert held.album_held() == frozenset()
    state = albums.State(albums={
        "A": {**statedb.new_record("A"), "status": "wanted", "origin": "wants",
              "playlists": {"disc": [["T", "Artist", "ID1"]]}},
        "W": {**statedb.new_record("W"), "status": "wanted", "playlists": {"albums": [["T", "Artist", "ID2"]]}}})
    state.in_db = True
    state.save()
    assert held.album_held() == {"https://open.spotify.com/track/ID1"}
    monkeypatch.setenv("ALBUM_MODE", "dry-run")
    assert held.album_held() == frozenset()

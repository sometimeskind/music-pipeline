"""music-audit-titles (homelab#2544): items titled as another version than Spotify's entry."""

from __future__ import annotations

import logging
import sys
from unittest import mock

import pytest

from music_fetch import store
from music_scan import titles

URL = "https://open.spotify.com/track/"


def _song(sid: str, name: str, album: str = "ALB", album_type: str = "album", date: str = "2015") -> dict:
    return {"name": name, "song_id": sid, "url": URL + sid, "album_id": album, "album_name": f"Album {album}",
            "album_type": album_type, "date": date}


def _item(item_id: int, title: str, sids: str | None, disambig: str = "", album: str = "Album", record=None,
          track: int = 1, disc: int = 1, spotify_album: str = "ALB"):
    fields = {"title": title, "trackdisambig": disambig, "spotify_ids": sids, "album": album,
              "albumartist": "Artist", "album_record": record, "track": track, "disc": disc,
              "spotify_album_id": spotify_album}
    item = mock.MagicMock(id=item_id)
    item.get.side_effect = lambda k, d=None: fields.get(k) or d
    return item


# The cases of homelab#2544, as shapes.
FLAGGED = [
    ("Oh! Sweet Nuthin' (5.1 mix)", "", "Oh! Sweet Nuthin' - 2015 Remaster", ["5.1"]),
    ("The Big Country (alternate version)", "", "The Big Country - Live", ["alternate"]),
    ("Purple Rain", "live, 1992‐05‐28: Ahoy Arena, Rotterdam, Netherlands", "Purple Rain", ["live"]),
    ("Purple Rain", "live, 1992‐05‐28: Ahoy Arena, Rotterdam, Netherlands", "Purple Rain - 2015 Remaster",
     ["live"]),
    ("Just Like a Woman (take 1, 3/08/1966, complete)", "",
     "Just Like a Woman - Live at Boston Music Hall, Boston, MA - November 1975", ["take"]),
    ("Live Forever (Live)", "", "Live Forever", ["live"]),
    ("Live Forever", "live, 1995-06-23: Glastonbury", "Live Forever - Remastered", ["live"]),
    ("Song (mono)", "", "Song - Remastered", ["mono"]),
    ("Song (demo)", "", "Song", ["demo"]),
    ("Song", "outtake", "Song", ["outtake"]),
]

NOT_FLAGGED = [
    ("Song (Live)", "", "Song - Live"),
    ("Purple Rain (live)", "live, 1992-05-28: Ahoy Arena", "Purple Rain - Live"),
    ("Purple Rain", "live, 1992-05-28: Ahoy Arena", "Purple Rain (Live at Ahoy)"),
    ("Live Forever", "", "Live Forever - Remastered"),
    ("Live Forever", "", "Live Forever"),
    ("Take Five", "", "Take Five"),
    ("Song (alternate take)", "", "Song - Alt. Take"),
    ("Song (alternative version)", "", "Song - Alternate Version"),
    ("Monotone Demolition", "", "Monotone Demolition"),
    ("Song (2015.1 edit)", "", "Song"),
    ("Symphony 5.10", "", "Symphony"),
]


@pytest.mark.parametrize(("title", "disambig", "spotify", "words"), FLAGGED)
def test_a_version_spotify_does_not_name_is_flagged(title, disambig, spotify, words) -> None:
    assert titles.differing(title, disambig, spotify) == words


@pytest.mark.parametrize(("title", "disambig", "spotify"), NOT_FLAGGED)
def test_a_version_spotify_names_too_or_a_song_name_is_not(title, disambig, spotify) -> None:
    assert titles.differing(title, disambig, spotify) == []


def test_a_version_spotifys_album_names_counts_as_spotifys() -> None:
    """A live album whose tracks Spotify titles bare: its trackdisambig "live, …" is the album's."""
    assert titles.differing("Psycho Killer", "live, 1977", "Psycho Killer", "Live at the Roxy") == []
    assert titles.differing("Psycho Killer (live)", "", "Psycho Killer", "Live at the Roxy") == []
    assert titles.differing("Just Like a Woman (take 1)", "", "Just Like a Woman",
                            "The Bootleg Series, Vol. 5: Live 1975") == ["take"]
    assert titles.differing("Purple Rain", "live, 1992", "Purple Rain", "Purple Rain (Deluxe)") == ["live"]


def test_version_part_is_after_the_dash_and_in_brackets() -> None:
    assert titles.version_part("Live Forever") == ""
    assert titles.version_part("Song - Live at X (Remastered)") == "Remastered Live at X (Remastered)"
    assert titles.version_part("Song – Mono") == "Mono"
    assert titles.version_part("Up-Tight") == ""


def test_the_slot_is_the_entry_on_the_items_spotify_album() -> None:
    by_id = titles.songs_by_id([_song("s1", "Song - Live", album="SINGLE", album_type="single"),
                                _song("a1", "Song", album="ALB"), _song("c1", "Song - Live", album="COMP",
                                                                         album_type="compilation", date="1990")])
    assert titles.spotify_entry(_item(1, "Song", "s1,a1,c1"), by_id)["song_id"] == "a1"
    # Off its album (or none): canon's preference, album over single over compilation.
    assert titles.spotify_entry(_item(2, "Song", "s1,c1", spotify_album="X"), by_id)["song_id"] == "s1"
    assert titles.spotify_entry(_item(3, "Song", "zz"), by_id) is None


def test_audit_counts_what_it_cannot_compare_and_groups_by_album() -> None:
    songs = [_song("a10", "Oh! Sweet Nuthin' - 2015 Remaster"), _song("a6", "Head Held High - 2015 Remaster"),
             _song("a7", "Fine"), _song("b8", "The Big Country - Live", album="B")]
    items = [
        _item(6930, "Oh! Sweet Nuthin' (5.1 mix)", "a10", album="Loaded", record="256", track=10),
        _item(6928, "Head Held High (5.1 mix)", "a6", album="Loaded", record="256", track=6),
        _item(6929, "Fine", "a7", album="Loaded", record="256", track=7),
        _item(8001, "The Big Country (alternate version)", "b8", album="Name of This Band", track=8,
              spotify_album="B"),
        _item(9001, "Track (5.1 mix)", None),  # no Spotify ID
        _item(9002, "Track (live)", "gone"),  # no store entry describes it
    ]
    report = titles.audit(items, songs)
    assert (report.compared, report.no_id, report.undescribed) == (4, 1, 1)
    albums = report.albums()
    assert [key for key, _ in albums] == ["256", ""]
    assert [f.id for f in albums[0][1]] == [6928, 6930]  # by track
    assert albums[1][1][0].words == ["alternate"] and albums[1][1][0].spotify == "The Big Country - Live"


def test_log_report_names_the_reset_and_each_item(caplog) -> None:
    songs = [_song("a10", "Oh! Sweet Nuthin' - 2015 Remaster")]
    report = titles.audit([_item(6930, "Oh! Sweet Nuthin' (5.1 mix)", "a10", album="Loaded", record="256",
                                 track=10, disambig="5.1 mix")], songs)
    records = {"256": {"artist": "Artist", "name": "Loaded"}}
    with caplog.at_level(logging.INFO, logger="music_scan.titles"):
        titles.log_report(report, records)
    assert "[ALBUM] Artist — Loaded (record 256): 1 item(s)" in caplog.text
    assert "music-album-reset 'Artist — Loaded' --reimport" in caplog.text
    assert ('[VERSION] 6930 1-10 "Oh! Sweet Nuthin\' (5.1 mix)" [trackdisambig: 5.1 mix] '
            'vs Spotify "Oh! Sweet Nuthin\' - 2015 Remaster": 5.1') in caplog.text
    assert "1 item(s) on 1 album(s)" in caplog.text and "of 1 compared" in caplog.text


def test_main_reads_the_library_and_the_stores(monkeypatch, caplog) -> None:
    store.save(store.Store.from_songs("keep", [_song("a1", "Song - 2015 Remaster")]))
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.all_items.return_value = [_item(1, "Song (5.1 mix)", "a1"), _item(2, "Other", None)]
    monkeypatch.setattr("music_scan.library.MusicLibrary", mock.MagicMock(return_value=lib))
    monkeypatch.setattr(sys, "argv", ["music-audit-titles"])
    with caplog.at_level(logging.INFO, logger="music_scan.titles"):
        titles.main()
    assert '[VERSION] 1 1-01 "Song (5.1 mix)" vs Spotify "Song - 2015 Remaster": 5.1' in caplog.text
    assert "of 1 compared; not compared: 1 with no Spotify ID, 0 whose IDs" in caplog.text

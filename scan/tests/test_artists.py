"""music-audit-artists (homelab#2649): items whose artist shares nothing with their album's."""

from __future__ import annotations

import logging
import sys
from unittest import mock

from music_scan import artists

SPRINGSTEEN, VOLTS = "70248960-cb53-4ea4-943a-edb18f7d336f", "16dcd746-37e9-40b7-83fb-8521f443af7a"
BORN = {"375": {"artist": "Bruce Springsteen", "name": "Born In The U.S.A.", "album_type": "album"}}


def _item(item_id: int, artist: str, albumartist: str = "Bruce Springsteen", album: str = "Born In The U.S.A.",
          record="375", track: int = 1, **fields):
    data = {"artist": artist, "albumartist": albumartist, "album": album, "album_record": record, "track": track,
            "title": f"Song {item_id}", "disc": 0, "via": "usenet", **fields}
    item = mock.MagicMock(id=item_id)
    item.get.side_effect = lambda k, d=None: data.get(k) or d
    return item


def test_the_case_is_listed_and_a_band_credit_is_not() -> None:
    """Album 375 as it stands: 18518 under 5000 Volts, 18513 a band credit."""
    report = artists.audit([
        _item(18518, "5000 Volts", track=6, mb_artistids=[VOLTS], mb_albumartistids=[SPRINGSTEEN]),
        _item(18513, "Bruce Springsteen & The E Street Band", mb_artistids=[SPRINGSTEEN], mb_albumartistids=[SPRINGSTEEN]),
        _item(18515, "Bruce Springsteen", track=3),
    ], BORN)
    assert [f.id for f in report.flagged] == [18518]
    assert report.compared == 3


def test_the_same_musicbrainz_artist_is_never_listed() -> None:
    """An alias shares no word, and its ID says it is one artist."""
    item = _item(1, "Yusuf", albumartist="Cat Stevens", record=None, mb_artistid="a", mb_albumartistid="a")
    assert artists.audit([item], {}).flagged == []


def test_a_compilation_is_left_out_and_counted() -> None:
    by_record = _item(1, "Someone", albumartist="Curator", record="7")
    by_flag = _item(2, "Someone", albumartist="Curator", album="Flagged", record=None, comp=True)
    by_name = _item(3, "Someone", albumartist="Various Artists", album="VA", record=None)
    by_type = _item(4, "Someone", albumartist="Curator", album="Typed", record=None, albumtypes=["album", "compilation"])
    report = artists.audit([by_record, by_flag, by_name, by_type], {"7": {"album_type": "compilation"}})
    assert report.flagged == [] and report.compilations == 4


def test_an_album_mostly_by_others_is_a_compilation_in_all_but_name() -> None:
    album = [_item(1, "One", albumartist="DJ Curator", record=None), _item(2, "Two", albumartist="DJ Curator", record=None),
             _item(3, "DJ Curator", albumartist="DJ Curator", record=None)]
    report = artists.audit(album, {})
    assert report.flagged == [] and report.mostly_others == 3


def test_one_other_artist_is_listed_however_small_the_album() -> None:
    """A loose track with a wrong match is the whole of its album."""
    report = artists.audit([_item(1, "5000 Volts", record=None)], {})
    assert [f.id for f in report.flagged] == [1]


def test_what_cant_be_compared_is_counted_not_listed() -> None:
    report = artists.audit([
        _item(1, "坂本龍一", albumartist="Ryuichi Sakamoto", record=None),
        _item(2, "Someone", albumartist="", album="No Album Artist", record=None),
        _item(3, "Someone", album="", record=None),
    ], {})
    assert report.flagged == []
    assert (report.unreadable, report.no_album, report.compared) == (2, 1, 0)


def test_the_records_artist_counts_as_the_albums() -> None:
    """Canon's album artist can be one spelling, the record's another."""
    item = _item(1, "Prince", albumartist="The Revolution", record="9")
    assert artists.audit([item], {"9": {"artist": "Prince & The Revolution"}}).flagged == []


def test_the_report_names_each_items_fix_and_the_count(caplog) -> None:
    report = artists.audit([_item(18518, "5000 Volts", track=6, title="I'm on Fire"),
                            _item(18515, "Bruce Springsteen", track=3)], BORN)
    with caplog.at_level(logging.INFO, logger="music_scan.artists"):
        artists.log_report(report)
    assert "[ALBUM] Bruce Springsteen — Born In The U.S.A. (record 375): 1 item(s)" in caplog.text
    assert ('[ARTIST] 18518 0-06 "I\'m on Fire" by "5000 Volts" (via usenet): recording_candidates 18518, '
            "then set_recording") in caplog.text
    assert "1 item(s) on 1 album(s) credited to an artist" in caplog.text and "of 2 compared" in caplog.text


def test_main_reads_the_library_and_the_album_store(monkeypatch, caplog) -> None:
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.all_items.return_value = [_item(1, "5000 Volts", record=None)]
    monkeypatch.setattr("music_scan.library.MusicLibrary", mock.MagicMock(return_value=lib))
    monkeypatch.setattr("music_fetch.albums.State.load", mock.MagicMock(return_value=mock.MagicMock(albums={})))
    monkeypatch.setattr(sys, "argv", ["music-audit-artists"])
    with caplog.at_level(logging.INFO, logger="music_scan.artists"):
        artists.main()
    assert "[ARTIST] 1 " in caplog.text

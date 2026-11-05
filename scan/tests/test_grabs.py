"""music-audit-grabs (homelab#2656): albums held from a release the album-name check refuses."""

from __future__ import annotations

import logging
import sys
from unittest import mock

from music_scan import grabs

EUROPE = "Curtis Mayfield-Live In Europe-CD-FLAC-1988-CALiFLAC"


def _record(artist: str, name: str, title: str | None, status: str = "imported", **fields) -> dict:
    return {"artist": artist, "name": name, "status": status, "spotify_album_ids": [],
            **({"candidate": {"title": title}} if title else {}), **fields}


def test_the_two_albums_are_listed_and_real_releases_are_not() -> None:
    report = grabs.audit({
        "670": _record("Curtis Mayfield", "Curtis Live! (US Release)", EUROPE),
        "1583": _record("Stan Getz", "Getz/Gilberto (Expanded Edition)",
                        "Stan Getz Astrud Gilberto-Starportrait-(2622002)-2LP-FLAC-1978-BITOCUL"),
        "1": _record("Curtis Mayfield", "Curtis Live!", "Curtis Mayfield-Curtis Live-CD-FLAC-1971-GRP"),
        "2": _record("Kate Bush", "The Sensual World", "Kate Bush-The Sensual World-NMR-1989-NMR"),
    }, {})
    assert [f.key for f in report.flagged] == ["670", "1583"]
    assert report.flagged[0].why == "curtis, beside in europe"
    assert report.compared == 4


def test_only_albums_the_library_holds_from_a_release_are_checked() -> None:
    wrong = ("Curtis Mayfield", "Curtis Live!", EUROPE)
    report = grabs.audit({
        "1": _record(*wrong, status="wanted"),
        "2": _record(*wrong, status="grabbed"),
        "3": _record(*wrong, status="fallback"),
        "4": _record(*wrong, status="filled", fallback_from="gap"),
        "5": _record(*wrong, status="filled", fallback_from="partial"),
        "6": _record(*wrong, status="fallback", fallback_from="partial"),
    }, {})
    assert [f.key for f in report.flagged] == ["5", "6"] and report.compared == 2
    assert report.skipped == {"wanted": 1, "grabbed": 1, "fallback": 1, "filled": 1}


def test_a_clean_result_says_what_it_left_out(caplog) -> None:
    """0 of N read as the check missing albums that had just been reset to wanted."""
    wrong = ("Curtis Mayfield", "Curtis Live!", EUROPE)
    report = grabs.audit({
        "1": _record(*wrong, status="wanted"),
        "2": _record(*wrong, status="wanted"),
        "3": _record(*wrong, status="failed"),
        "4": _record("Kate Bush", "The Sensual World", "Kate Bush-The Sensual World-NMR-1989-NMR"),
    }, {})
    assert report.flagged == [] and report.compared == 1
    with caplog.at_level(logging.INFO, logger="music_scan.grabs"):
        grabs.log_report(report)
    assert "of 1 compared" in caplog.text
    assert "not held from a release, so not read: 2 wanted, 1 failed" in caplog.text
    assert "A clean result says nothing about the albums not compared or not read" in caplog.text


def test_what_cant_be_compared_is_counted() -> None:
    report = grabs.audit({
        "1": _record("Curtis Mayfield", "Curtis Live!", None),
        "2": _record("Curtis Mayfield", "Curtis Live!", EUROPE),
        "3": _record("Curtis Mayfield", "★★★", EUROPE),
    }, {"2": "curtis mayfield live"})
    assert report.flagged == [] and report.compared == 0
    assert (report.no_title, report.no_artist) == (1, 2)


def test_main_reads_the_album_store_and_logs_each_album_and_the_count(monkeypatch, caplog) -> None:
    state = mock.MagicMock(albums={"670": _record("Curtis Mayfield", "Curtis Live! (US Release)", EUROPE)})
    monkeypatch.setattr("music_fetch.albums.State.load", mock.MagicMock(return_value=state))
    monkeypatch.setattr("music_fetch.albums._search_overrides", mock.MagicMock(return_value={}))
    monkeypatch.setattr(sys, "argv", ["music-audit-grabs"])
    with caplog.at_level(logging.INFO, logger="music_scan.grabs"):
        grabs.main()
    assert f'[NOTALBUM] 670: Curtis Mayfield — Curtis Live! (US Release) came from "{EUROPE}"' in caplog.text
    assert "1 album(s) held from a release the album-name check refuses, of 1 compared" in caplog.text

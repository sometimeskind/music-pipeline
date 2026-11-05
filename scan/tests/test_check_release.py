"""music-check-release (homelab#2661): the matcher's verdict on one release for one album."""

from __future__ import annotations

import logging
import sys
from unittest import mock

import pytest

from music_fetch.usenet import size_range
from music_scan import check_release

SON = "Harry Nilsson - Son of Schmilsson - NMR"
WALKMAN = "Harry.Nilsson-Nilsson.Schmilsson-16BIT-44-KHZ-WEB-FLAC-1971-WALKMAN"
KEY = "1734"


def _record(artist: str = "Harry Nilsson", name: str = "Nilsson Schmilsson", **fields) -> dict:
    return {"artist": artist, "name": name, "status": "wanted", "tracks_count": 12, "album_type": "album",
            "duration": 2040, "blocklist": [], "playlists": {}, "spotify_album_ids": ["SPOTIFYID"], **fields}


def _check(title: str, overrides: dict | None = None, record: dict | None = None, **kwargs) -> check_release.Check:
    return check_release.check(KEY, record or _record(), title, overrides or {}, **kwargs)


def _output(caplog, result: check_release.Check) -> str:
    with caplog.at_level(logging.INFO, logger="music_scan.check_release"):
        check_release.log_check(result)
    return caplog.text


def test_another_album_by_the_artist_is_refused_with_the_rule() -> None:
    why = _check(SON).why
    assert why is not None and why.rule == "artist words" and why.detail.startswith("nilsson, beside ")


def test_the_real_release_is_accepted() -> None:
    assert _check(WALKMAN).why is None


def test_override_words_are_simulated_through_the_plan(caplog) -> None:
    result = _check(WALKMAN, words="nilsson schmilsson son")
    assert result.search.queries == ["nilsson schmilsson son"] and result.search.artist == ""
    assert (result.why.rule, result.why.detail) == ("album words", "lacks son")
    output = _output(caplog, result)
    assert 'Override: simulated "nilsson schmilsson son", none stored' in output
    assert "the artist words and self-titled rules don't run" in output


def test_an_override_turns_the_artist_words_rule_off() -> None:
    """The album's own check refuses this title; an override holding its words lets it through."""
    assert _check(SON).why.rule == "artist words"
    assert _check(SON, words="harry nilsson schmilsson").why is None


def test_an_override_must_hold_a_word_the_title_starts_with() -> None:
    """No artist to read off the title, so words that skip its first one refuse the real release."""
    why = _check(WALKMAN, words="nilsson schmilsson 1971").why
    assert (why.rule, why.detail) == ("artist", "doesn't start with an album word")
    assert _check(WALKMAN, words="harry nilsson schmilsson 1971").why is None


def test_the_arguments_are_the_ticks(monkeypatch) -> None:
    spy = mock.MagicMock(return_value=None)
    monkeypatch.setattr(check_release, "rejection", spy)
    _check(WALKMAN, size=900_000_000)
    (release, artist, album, tracks), kwargs = spy.call_args
    assert (release.title, release.size) == (WALKMAN, 900_000_000)
    assert (artist, album, tracks) == ("Harry Nilsson", "Nilsson Schmilsson", 12)
    assert kwargs == {"album_type": "album", "seconds": 2040, "any_album": False}


def test_tracks_fall_back_to_the_longest_playlist_as_the_tick_counts_them() -> None:
    record = _record(tracks_count=0, duration=0, album_type=None, playlists={"a": ["1"], "b": ["1", "2", "3", "4"]})
    assert _check(WALKMAN, record=record).high == size_range(4)[1]


def test_no_size_never_refuses_on_size_and_says_it_wasnt_checked(caplog) -> None:
    result = _check(WALKMAN)
    assert result.why is None and not result.size_judged
    output = _output(caplog, result)
    assert "Verdict: accepted on the title; size not checked" in output
    assert "Size: not checked: no size given; this album takes 33 MB to 2040 MB" in output


def test_no_size_keeps_the_track_count_for_the_single_rule() -> None:
    """Size unknown must not mean tracks unknown: the matcher reads them for `single` too."""
    title = "Harry.Nilsson-Nilsson.Schmilsson-EP-WEB-FLAC-1971-WALKMAN"
    assert _check(title, record=_record(album_type=None)).why.rule == "single"
    assert _check(title, record=_record(album_type=None, tracks_count=3, duration=0)).why is None


def test_a_given_size_is_judged(caplog) -> None:
    fits = _check(WALKMAN, size=400_000_000)
    assert fits.why is None and fits.size_judged
    assert "Verdict: accepted\n" in _output(caplog, fits) + "\n"
    assert "Size: checked: 400 MB, and this album takes 33 MB to 2040 MB" in caplog.text
    small = _check(WALKMAN, size=1_000_000)
    assert small.why.rule == "size" and small.size_judged


def test_a_title_refused_first_leaves_the_given_size_unjudged(caplog) -> None:
    output = _output(caplog, _check(SON, size=1_000_000))
    assert "Verdict: refused by artist words" in output and "later rules weren't run" in output
    assert "Size: not checked: the title was refused first" in output


def test_an_album_of_unknown_length_has_no_size_rule(caplog) -> None:
    result = _check(WALKMAN, record=_record(tracks_count=0, duration=0), size=1)
    assert result.why is None
    output = _output(caplog, result)
    assert "accepted on the title" not in output and "the tick has no size rule for it" in output


def test_a_stored_override_applies_and_is_named(caplog) -> None:
    result = _check(WALKMAN, {"SPOTIFYID": "harry nilsson schmilsson"})
    assert result.why is None and result.search.queries == ["harry nilsson schmilsson"]
    assert 'Override: stored "harry nilsson schmilsson" applied' in _output(caplog, result)


def test_simulated_words_stand_in_for_the_stored_override(caplog) -> None:
    result = _check(WALKMAN, {KEY: "nilsson schmilsson 1971"}, words="schmilsson nilsson")
    assert result.search.queries == ["schmilsson nilsson"]
    assert 'simulated "schmilsson nilsson", in place of the stored "nilsson schmilsson 1971"' in _output(caplog, result)


def test_no_override_is_said_and_the_plans_queries_shown(caplog) -> None:
    output = _output(caplog, _check(WALKMAN))
    assert "Override: none stored" in output
    assert 'Search: "harry nilsson nilsson schmilsson", then "harry nilsson"' in output
    assert "Not covered: the blocklist" in output


def test_a_name_with_no_words_searches_the_artist_and_says_what_matches(caplog) -> None:
    output = _output(caplog, _check("Harry Nilsson-Anything-FLAC", record=_record(name="★★★")))
    assert 'Search: "harry nilsson": any release by the artist matches' in output


def test_nothing_to_search_is_said(caplog) -> None:
    output = _output(caplog, _check(WALKMAN, record=_record(artist="★★★", name="♫♫♫")))
    assert "Search: none" in output and "album words" in output


def test_an_edition_pinned_by_an_incomplete_copy_is_said(caplog) -> None:
    output = _output(caplog, _check(WALKMAN, record=_record(edition=["deluxe"])))
    assert "the tick takes only its edition (deluxe); this title's is (plain)" in output


def _main(monkeypatch, *argv: str, records: dict | None = None) -> mock.MagicMock:
    state = mock.MagicMock(albums={KEY: _record()} if records is None else records)
    monkeypatch.setattr("music_fetch.albums.State.load", mock.MagicMock(return_value=state))
    monkeypatch.setattr("music_fetch.albums._search_overrides", mock.MagicMock(return_value={}))
    monkeypatch.setattr(sys, "argv", ["music-check-release", *argv])
    check_release.main()
    return state


def test_main_reads_the_store_writes_nothing_and_logs_the_verdict(monkeypatch, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="music_scan.check_release"):
        state = _main(monkeypatch, f"--album={KEY}", f"--title={WALKMAN}", "--words=Nilsson Schmilsson -Son")
    assert state.method_calls == []
    assert "Verdict: refused by album words: lacks son" in caplog.text
    assert '[CHECK] 1734: Harry Nilsson — Nilsson Schmilsson (wanted): "' in caplog.text


def test_main_takes_a_word_that_starts_with_a_dash(monkeypatch, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="music_scan.check_release"):
        _main(monkeypatch, f"--album={KEY}", f"--title={WALKMAN}", "--words=-Son", "--size=400000000")
    assert 'simulated "son"' in caplog.text


def test_main_refuses_unreadable_words(monkeypatch) -> None:
    with pytest.raises(SystemExit):
        _main(monkeypatch, f"--album={KEY}", f"--title={WALKMAN}", "--words=♫")


def test_main_stops_when_the_album_isnt_one(monkeypatch, caplog) -> None:
    with pytest.raises(SystemExit) as stopped:
        _main(monkeypatch, "--album=nothing", f"--title={WALKMAN}")
    assert stopped.value.code == 1 and "0 album(s) match" in caplog.text

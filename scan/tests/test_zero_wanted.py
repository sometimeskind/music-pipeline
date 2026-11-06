"""music-audit-zero-wanted (homelab#2662): albums imported with none of their wanted tracks found."""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone

from music_fetch import albums, events
from music_scan import zero_wanted

AT = datetime(2026, 11, 1, 20, 52, tzinfo=timezone.utc)
SECRET_LOVE = ("[DONE] Dry Cleaning — Secret Love (Deluxe Edition) (wants: fancy, discoveries): 11 file(s) from "
               "Dry Cleaning-Secret Love-16BIT-WEB-FLAC-2026-OBZEN; 2 of its 2 wanted track(s) not in this release "
               "go back to spotdl")
CURTIS = ("[DONE] Curtis Mayfield — Curtis Live! (US Release) (wants: fancy): 13 file(s) from Curtis Mayfield-Live In "
          "Europe-CD-FLAC-1988-CALiFLAC; 2 of its 3 wanted track(s) not in this release go back to spotdl")
WHOLE = "[DONE] Steely Dan — Aja (wants: fancy, orbit): 7 file(s) from Steely Dan-Aja-REPACK-CD-FLAC-1977-QoQ"
NO_COPY = ("[DONE] Herbie Hancock — Head Hunters (wants: ritzy): no other copy of Herbie's edition; it is the album, "
           "lacking 2 track(s) of its own release")


def _record(name: str = "Secret Love (Deluxe Edition)", status: str = "imported", **fields) -> dict:
    return {"artist": "Dry Cleaning", "name": name, "status": status, "spotify_album_ids": [],
            "candidate": {"title": "Dry Cleaning-Secret Love-16BIT-WEB-FLAC-2026-OBZEN"}, **fields}


def _row(key: str, message: str, at: datetime = AT, spotify: str | None = None, **data) -> tuple:
    return at, spotify, message, {"key": key, **data}


def test_the_line_and_the_fields_give_the_count() -> None:
    assert zero_wanted.counted(SECRET_LOVE, {}) == (0, 2)
    assert zero_wanted.counted(CURTIS, {}) == (1, 3)
    assert zero_wanted.counted("[DONE] A — B: 3 file(s) from x; 3 of Spotify's 3 titles not in this release", {}) \
        == (0, 3)
    assert zero_wanted.counted(WHOLE, {}) == (0, 0)
    assert zero_wanted.counted(NO_COPY, {"why": "no other copy of the edition"}) is None


def test_stored_fields_win_over_the_sentence() -> None:
    assert zero_wanted.counted(SECRET_LOVE, {"found": 1, "total": 2}) == (1, 2)
    assert zero_wanted.counted(WHOLE, {"found": 0, "total": 2, "why": "edition lacks the same tracks"}) == (0, 2)


def test_only_zero_of_n_is_listed() -> None:
    report = zero_wanted.audit({"770": _record(), "670": _record("Curtis Live!"), "797": _record("Aja")},
                               [_row("770", SECRET_LOVE), _row("670", CURTIS), _row("797", WHOLE)])
    assert [(f.key, f.total) for f in report.flagged] == [("770", 2)]
    assert report.compared == 3


def test_the_last_import_decides() -> None:
    later = AT.replace(day=5)
    report = zero_wanted.audit({"770": _record(), "1": _record("Other")},
                               [_row("770", SECRET_LOVE), _row("770", WHOLE, later),
                                _row("1", WHOLE), _row("1", SECRET_LOVE, later)])
    assert [f.key for f in report.flagged] == ["1"] and report.flagged[0].at == later


def test_an_event_names_an_album_by_any_of_its_refs() -> None:
    record = _record(spotify_album_ids=["75bQmXdU9gLCW3CG79wKBF"])
    assert [f.key for f in zero_wanted.audit({"770": record}, [_row("75bQmXdU9gLCW3CG79wKBF", SECRET_LOVE)]).flagged] \
        == ["770"]
    report = zero_wanted.audit({"770": record}, [(AT, "75bQmXdU9gLCW3CG79wKBF", SECRET_LOVE, {})])
    assert [f.key for f in report.flagged] == ["770"]


def test_a_kept_copy_is_named_by_its_own_title() -> None:
    row = _row("770", "[DONE] x: the most complete copy, Kept, lacks 1 track(s) of its own release", found=0, total=2,
               why="the most complete copy after the last attempt", kept="Kept-FLAC")
    assert zero_wanted.audit({"770": _record()}, [row]).flagged[0].title == "Kept-FLAC"


def test_a_release_is_named_by_its_import_not_a_later_candidate() -> None:
    message = ("[DONE] Paul McCartney — McCartney (Archive Collection) (wants: soft): 11 file(s) from "
               "Paul McCartney-McCartney III-24-96-WEB-FLAC-2020-OBZEN; 2 of its 2 wanted track(s) not in this "
               "release go back to spotdl")
    record = _record(candidate={"title": "Paul McCartney-Oceans Kingdom-24-96-WEB-FLAC-2011-OBZEN"})
    assert zero_wanted.audit({"813": record}, [_row("813", message)]).flagged[0].title \
        == "Paul McCartney-McCartney III-24-96-WEB-FLAC-2020-OBZEN"


def test_a_message_naming_no_release_falls_back_to_the_candidate() -> None:
    row = _row("770", "[DONE] x: the most complete copy, Kept, lacks 1 track(s) of its own release", found=0, total=2)
    assert zero_wanted.audit({"770": _record()}, [row]).flagged[0].title \
        == "Dry Cleaning-Secret Love-16BIT-WEB-FLAC-2026-OBZEN"


def test_what_cant_be_compared_is_counted() -> None:
    report = zero_wanted.audit({
        "1": _record(),
        "2": _record(),
        "3": _record(status="wanted"),
        "4": _record(status="filled", fallback_from="gap"),
        "5": _record(status="filled", fallback_from="partial"),
    }, [_row("2", NO_COPY, why="no other copy of the edition"), _row("3", SECRET_LOVE), _row("5", SECRET_LOVE)])
    assert [f.key for f in report.flagged] == ["5"] and report.compared == 1
    assert (report.no_event, report.no_count) == (1, 1)
    assert report.skipped == {"wanted": 1, "filled": 1}


def test_the_output_says_a_listed_album_may_be_right_and_what_it_cant_see(caplog) -> None:
    report = zero_wanted.audit({"770": _record(), "3": _record(status="wanted")}, [_row("770", SECRET_LOVE)])
    with caplog.at_level(logging.INFO, logger="music_scan.zero_wanted"):
        zero_wanted.log_report(report)
    assert ('[NOWANTED] 770: Dry Cleaning — Secret Love (Deluxe Edition): 0 of its 2 wanted track(s) in '
            '"Dry Cleaning-Secret Love-16BIT-WEB-FLAC-2026-OBZEN", imported 2026-11-01') in caplog.text
    assert "1 album(s) imported with 0 of their wanted tracks found, of 1 compared" in caplog.text
    assert "not read: 1 wanted" in caplog.text
    assert "A listed album may be right: title formats can differ" in caplog.text
    assert "can't see an import before 2026-10-29" in caplog.text
    assert "nor a wrong album sharing one wanted track" in caplog.text


def test_it_reads_the_event_store(monkeypatch, caplog) -> None:
    """The real tables: the audit's query, end to end, from main."""
    albums.State(albums={"a0": _record(spotify_album_ids=["a0"]),
                         "a1": _record("Aja", spotify_album_ids=["a1"])}).save()
    events.record("import_done", SECRET_LOVE, source="albums", data={"key": "a0"})
    events.record("import_done", WHOLE, source="albums", data={"key": "a1"})
    events.record("import_done", SECRET_LOVE, source="albums", data={"key": "gone"})
    monkeypatch.setattr(sys, "argv", ["music-audit-zero-wanted"])
    with caplog.at_level(logging.INFO, logger="music_scan.zero_wanted"):
        zero_wanted.main()
    assert ": Dry Cleaning — Secret Love (Deluxe Edition): 0 of its 2 wanted track(s)" in caplog.text
    assert "1 album(s) imported with 0 of their wanted tracks found, of 2 compared" in caplog.text


def test_it_never_loads_beets() -> None:
    """An audit holding the beets database crashed imports (homelab#2665)."""
    code = "import sys, music_scan.zero_wanted; sys.exit(any(m.split('.')[0] == 'beets' for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0

"""music-album-blocklist (homelab#2750): a release blocklisted by hand, which no
reset clears.  The album is the case the issue names, anonymised: a plain title
the matcher accepts, imported, that held another album's files."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from music_fetch import albums, blocklist, blocklist_cli, events
from music_fetch import state as statedb
from music_fetch.albums import Settings, State, top_up
from music_fetch.usenet import Release

SID = "6dQ1LBMv0Yhzrb8IHJ0gLl"
PLAIN = "Artist-Album.1987.FLAC"
PLAIN_GUID = "https://indexer/details/plain"
DEAD = "Artist-Album-(651119 6)-VINYL-FLAC-1987-GRP"
DEAD_GUID = "https://indexer/details/dead"
REPAIR = "Repair failed, not enough repair blocks (15 short)"
WHY = "another album in it: 31 items by other artists"
AT = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
HAND_REACH = " (blocklisted by hand, so every copy of it, on any indexer; no reset clears it)"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    with patch.object(albums, "_push"):
        yield


def album(**fields) -> dict:
    return {"artist": "Artist", "name": "Album", "status": albums.IMPORTED, "attempts": 2, "album_type": "album",
            "tracks_count": 10, "duration": 2365, "origin": albums.WANTS, "year": "1987", "spotify_album_ids": [SID],
            "playlists": {"cool": [["Album", "Artist", "x", "y", 1, 1]]}, "blocklist": [DEAD_GUID],
            "candidate": {"guid": PLAIN_GUID, "title": PLAIN, "size": 869849295}, **fields}


def row(minutes: int, kind: str, **data) -> tuple:
    return AT + timedelta(minutes=minutes), kind, data


ROWS = [row(0, "grab", guid=DEAD_GUID, release=DEAD, size=237966466),
        row(1, "failure", reason=REPAIR, guid=DEAD_GUID),
        row(2, "grab", guid=PLAIN_GUID, release=PLAIN, size=869849295)]
HAND = [*ROWS, row(3, "blocklisted", guid=PLAIN_GUID, release=PLAIN, reason=f"{blocklist.BY_HAND}{WHY}")]


# ---------------------------------------------------------------------------
# The kind (no database)
# ---------------------------------------------------------------------------


def nest(title: str = PLAIN, guid: str = "https://nzbnest/details/plain") -> Release:
    """Another indexer's copy of a release: a GUID of its own, a size the album takes."""
    return Release(guid=guid, title=title, size=300_000_000, indexer_id=2, download_url="d", categories=[3040])


def test_a_hand_entry_is_its_own_kind_and_stays():
    entry = {"reason": f"{blocklist.BY_HAND}{WHY}"}
    assert blocklist.kind(entry) == blocklist.HAND
    assert blocklist.stays(entry)


def test_a_hand_entry_blocks_every_copy_of_its_title():
    record = album(blocklist=[DEAD_GUID, PLAIN_GUID])
    dead, plain = listed = blocklist.entries(record, HAND)
    assert (plain["kind"], plain["every_copy"], plain["reason"]) == (blocklist.HAND, True, f"by hand: {WHY}")
    assert (dead["kind"], dead["every_copy"]) == (blocklist.DEAD_DOWNLOAD, False)
    copies = blocklist.blocked(listed)
    assert nest() in copies and nest(f"{PLAIN}-1") in copies and nest(DEAD) not in copies
    assert blocklist.describe(plain) == f"{PLAIN} (870 MB): by hand: {WHY}{HAND_REACH}"


def test_a_hand_entry_with_no_title_blocks_only_its_guid():
    (entry,) = blocklist.entries(album(blocklist=[PLAIN_GUID], candidate={}),
                                 [row(0, "blocklisted", guid=PLAIN_GUID, reason=f"{blocklist.BY_HAND}{WHY}")])
    assert entry["kind"] == blocklist.HAND and not entry["every_copy"]
    assert blocklist.reach(entry) == "blocklisted by hand, so only this copy: its title isn't recorded; no reset clears it"


def test_a_reset_keeps_a_hand_entry_even_as_the_last_candidate(monkeypatch):
    monkeypatch.setattr(albums, "blocklist_entries", lambda key, record: blocklist.entries(record, HAND))
    for reimport in (False, True):
        state = State(albums={"K": album(blocklist=[DEAD_GUID, PLAIN_GUID])})
        _, _, after = albums.reset_album(state, "K", reimport=reimport)
        assert after["blocklist"] == [DEAD_GUID, PLAIN_GUID]


def test_the_ticks_automatic_reimport_keeps_a_hand_entry_and_grabs_another_release(monkeypatch, caplog):
    """The last candidate leaves the blocklist at an [AUTORESET]: not when it was blocklisted by hand."""
    monkeypatch.setattr(albums, "blocklist_entries", lambda key, record: blocklist.entries(record, HAND))
    record = album(blocklist=[DEAD_GUID, PLAIN_GUID], duration=0)
    state = State(albums={"K": record})
    albums.auto_reimport(state, Settings(mode="on"), lambda key, r: ["1 spotdl track(s) on the album"], set(),
                         albums.TickResult(), now=lambda: NOW)
    assert record["reimport"] and record["blocklist"] == [DEAD_GUID, PLAIN_GUID]

    other = Release(guid="https://indexer/details/other", title="Artist-Album-WEB-FLAC-2011-GRP", size=300_000_000,
                    indexer_id=1, download_url="d", categories=[3040])
    prowlarr, sab = MagicMock(), MagicMock()
    prowlarr.blocked.return_value = []
    prowlarr.nzb_location.return_value = "https://indexer/getnzb?id=1"
    sab.add_url.return_value = "nzo"
    prowlarr.search.return_value = [nest(), nest(f"{PLAIN}-1", "https://indexer/details/repost"), other]
    top_up(state, Settings(mode="on"), prowlarr, sab, lambda key, r: True, albums.TickResult(), now=lambda: NOW)
    assert record["status"] == albums.GRABBED and record["candidate"]["guid"] == other.guid


def test_with_only_copies_of_a_hand_entry_left_a_reimport_stays(monkeypatch, caplog):
    monkeypatch.setattr(albums, "blocklist_entries", lambda key, record: blocklist.entries(record, HAND))
    record = album(blocklist=[DEAD_GUID, PLAIN_GUID], duration=0)
    state = State(albums={"K": record})
    albums.reset_album(state, "K", reimport=True)
    prowlarr, sab = MagicMock(), MagicMock()
    prowlarr.blocked.return_value = []
    prowlarr.search.return_value = [nest(), nest(f"{PLAIN}-1", "https://indexer/details/repost")]
    with caplog.at_level(logging.INFO, logger="music_fetch.albums"):
        top_up(state, Settings(mode="on"), prowlarr, sab, lambda key, r: True, albums.TickResult(), now=lambda: NOW)
    assert record["status"] == albums.IMPORTED and "[STAYS] Artist — Album" in caplog.text
    assert "rejected: 2 blocklisted" in caplog.text  # the blocklist refused them, not the matcher
    sab.add_url.assert_not_called()


# ---------------------------------------------------------------------------
# The command (the music database)
# ---------------------------------------------------------------------------


def seed(rows: list[tuple] = ROWS, **fields) -> str:
    """The album and its events; returns its key."""
    State(albums={"1454": album(**fields)}).save()
    for at, kind, data in rows:
        assert events.record(kind, f"[{kind}]", source="albums", spotify_album_id=SID, data={"key": "1454", **data})
    (key,) = State.load().albums
    return key


def stored() -> dict:
    (record,) = State.load().albums.values()
    return record


def run(caplog, *argv: str) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_fetch.blocklist_cli"):
        blocklist_cli.main(list(argv))
    return [r.getMessage() for r in caplog.records if r.name == "music_fetch.blocklist_cli"]


def album_events(kind: str) -> list[tuple]:
    with statedb.session() as conn:
        return conn.execute("SELECT caller, job_id, message, data FROM events WHERE kind = %s ORDER BY id",
                            (kind,)).fetchall()


def test_the_dry_run_names_the_release_and_the_reason_and_writes_nothing(caplog):
    key = seed()
    lines = run(caplog, "add", f"--reason={WHY}", "--", key, PLAIN)
    assert lines == [
        f"Album: Artist — Album [{key}; imported; 1 blocklisted]",
        f"Release: {PLAIN} (870 MB) [{PLAIN_GUID}]",
        f"Blocklist: not on it → by hand: {WHY}{HAND_REACH}",
        "Effect: none until the album is searched again: it is imported, and was imported from this release. No "
        "reset clears this entry: reset_album with reimport=true then grabs the best other release the matcher "
        "accepts, or with none left logs [STAYS] and leaves the album as it is.",
        "Dry run; --apply to write"]
    assert run(caplog, "add", f"--reason={WHY}", "--", key, PLAIN_GUID) == lines  # by GUID: the same release
    assert stored()["blocklist"] == [DEAD_GUID] and album_events("blocklisted") == []


def test_the_apply_blocklists_it_with_its_event(caplog, monkeypatch):
    key = seed()
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "sub-tom")
    monkeypatch.setenv("MUSIC_EVENT_JOB", "job-1")
    lines = run(caplog, "add", f"--reason=  {WHY} ", "--apply", "--", key, PLAIN)
    assert f"[BLOCKLIST] Artist — Album (wants: cool) ({key}): {PLAIN} blocklisted by sub-tom: {WHY}" in lines
    assert stored()["blocklist"] == [DEAD_GUID, PLAIN_GUID]
    ((caller, job, message, data),) = album_events("blocklisted")
    assert (caller, job) == ("sub-tom", "job-1") and message.startswith("[BLOCKLIST]")
    assert data == {"album": "Artist — Album", "key": key, "origin": albums.WANTS, "release": PLAIN,
                    "guid": PLAIN_GUID, "size": 869849295, "reason": f"by hand: {WHY}", "was": None}
    entry = albums.blocklist_entries(key, stored())[1]
    assert (entry["kind"], entry["every_copy"]) == (blocklist.HAND, True)
    # Once on it, the same request changes nothing.
    again = run(caplog, "add", f"--reason={WHY}", "--apply", "--", key, PLAIN)
    assert again[2] == (f"Blocklist: blocklisted by hand already: {PLAIN} (870 MB): by hand: {WHY}{HAND_REACH}. "
                        "Nothing to change (unblocklist_release first for another reason)")
    assert len(album_events("blocklisted")) == 1


def test_a_reset_lists_it_as_staying_and_keeps_it(caplog, monkeypatch):
    key = seed()
    run(caplog, "add", f"--reason={WHY}", "--apply", "--", key, PLAIN)
    monkeypatch.setattr("sys.argv", ["music-album-reset", "--reimport", "--apply", key])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_fetch.albums"):
        albums.reset_main()
    text = caplog.text
    assert "Stays on the blocklist (" in text
    assert f"  {PLAIN} (870 MB): by hand: {WHY}{HAND_REACH}" in text
    record = stored()
    assert record["blocklist"] == [DEAD_GUID, PLAIN_GUID] and record["reimport"] is True


def test_a_wrong_release_on_the_list_already_becomes_a_hand_entry(caplog):
    wrong = "another album: 0 of the album's 10 titles in it"
    key = seed([*ROWS, row(3, "failure", reason=wrong, guid=PLAIN_GUID)], blocklist=[DEAD_GUID, PLAIN_GUID])
    lines = run(caplog, "add", f"--reason={WHY}", "--apply", "--", key, PLAIN)
    assert (f"Blocklist: on it as {wrong} (a wrong release, so every copy of it, on any indexer) → "
            f"by hand: {WHY}{HAND_REACH}") in lines
    assert stored()["blocklist"] == [DEAD_GUID, PLAIN_GUID]
    assert album_events("blocklisted")[0][3]["was"] == wrong


def test_a_release_the_album_never_grabbed_is_refused_with_its_releases(caplog):
    key = seed()
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as refused:
        blocklist_cli.main(["add", "--reason=x", "--", key, "Artist-Album-OTHER"])
    assert refused.value.code == 1
    assert f"no release in album {key}'s history is 'Artist-Album-OTHER'" in caplog.text
    assert f"{DEAD} [{DEAD_GUID}]" in caplog.text and f"{PLAIN} [{PLAIN_GUID}]" in caplog.text


def test_a_release_in_flight_is_refused(caplog):
    key = seed(status=albums.GRABBED, nzo_id="nzo")
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        blocklist_cli.main(["add", "--reason=x", "--apply", "--", key, PLAIN])
    assert "is in flight" in caplog.text and stored()["blocklist"] == [DEAD_GUID]


def test_a_wanted_albums_next_search_skips_it(caplog):
    key = seed(status=albums.WANTED)
    assert "Effect: the album's next search skips it." in run(caplog, "add", "--reason=x", "--", key, PLAIN)


def test_remove_takes_a_hand_entry_off_and_nothing_else(caplog, monkeypatch):
    key = seed()
    run(caplog, "add", f"--reason={WHY}", "--apply", "--", key, PLAIN_GUID)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        blocklist_cli.main(["remove", "--apply", "--", key, DEAD])
    assert "is not blocklisted by hand" in caplog.text and "nothing clears a dead download" in caplog.text
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "sub-tom")
    dry = run(caplog, "remove", "--", key, PLAIN)
    assert f"Blocklist: by hand: {WHY}{HAND_REACH} → off it; the matcher accepts it again" in dry
    assert run(caplog, "remove", "--", key, PLAIN) == dry
    run(caplog, "remove", "--apply", "--", key, PLAIN)
    assert stored()["blocklist"] == [DEAD_GUID]
    ((caller, _, message, data),) = album_events("unblocklisted")
    assert caller == "sub-tom" and message.startswith("[UNBLOCKLIST]")
    assert (data["guid"], data["reason"]) == (PLAIN_GUID, f"by hand: {WHY}")

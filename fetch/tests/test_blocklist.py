"""What a blocklist's GUIDs were and why (homelab#2661): the join with the event
store, and what the reset and override dry runs say from it.  The titles, sizes
and reasons are album 1274's: Nilsson Schmilsson, two broken downloads, then
another album imported."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from music_fetch import albums, blocklist, events, overrides_cli, state
from music_fetch.albums import State
from music_fetch.usenet import Release, exclusion, leading_word, rejection

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

SID = "3EfpOFKjotrMQTFTnxrXaB"
WALKMAN = "https://indexer/details/walkman"
JAPANESE = "https://indexer/details/japanese"
SON = "https://indexer/details/son"
# NZBNest's copy of the WALKMAN upload (homelab#2757): a GUID of its own, the same title.
NEST = "https://nzbnest/details/walkman"
WALKMAN_TITLE = "Harry.Nilsson-Nilsson.Schmilsson-16BIT-44-KHZ-WEB-FLAC-1971-WALKMAN"
JAPANESE_TITLE = "Harry Nilsson  1971 - Nilsson Schmilsson (Japanese issue)"
SON_TITLE = "Harry Nilsson - Son of Schmilsson - NMR"
ENCRYPTED = "Aborted, encryption detected"
REPAIR = "Repair failed, not enough repair blocks (1238 short)"
WRONG = "another album: 0 of the album's 10 titles in it"
LACKS = "lacks 2 track(s) of its own release"
AT = datetime(2026, 11, 3, 9, 52, tzinfo=timezone.utc)
# What release.retire() does: an import retires what the new release holds; a reimport also another album (homelab#2662).
OLD_ITEMS = ("Nothing is deleted now: the album's old items stay until an import. That import moves to "
             "quarantine/replaced/ the ones whose recording the new release also holds, and every item of the "
             "album's old release when it holds under half of the new release's titles: it is another album. "
             "Otherwise the rest stay on the album as local extras. Extras from an earlier import aren't judged: "
             "they stay unless the new release holds their recording, and retire_item takes them off. A playlist "
             "entry only a moved item held goes empty: a spotdl playlist downloads it at the next nightly, an "
             "album playlist's slot stays empty.")
STAYS = ("Stays on the blocklist (a reset clears only another album or an incomplete copy, and only reimport=true "
         "more than the last candidate):")
# What each kind of entry blocks, as the dry run says (homelab#2757).
ONE_DEAD = " (a dead download, so only this copy: another indexer's copy may be tried once)"
EVERY_WRONG = " (a wrong release, so every copy of it, on any indexer)"
UNTITLED_WRONG = " (a wrong release, but only this copy: its title isn't recorded)"
PLAIN_OLD_ITEMS = ("Nothing is deleted now: the album's old items stay until an import. That import retires only the "
                   "ones whose recording the new release also holds; the rest stay on the album as local extras, "
                   "which retire_item takes off.")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")


def nilsson(**fields) -> dict:
    return {"artist": "Harry Nilsson", "name": "Nilsson Schmilsson", "status": albums.IMPORTED, "attempts": 2,
            "album_type": "album", "duration": 2815, "tracks_count": 10, "origin": albums.WANTS, "year": "1971",
            "spotify_album_ids": [SID], "blocklist": [WALKMAN, JAPANESE],
            "playlists": {"cool": [["Jump Into The Fire", "Harry Nilsson", "x", "y", 1, 9]]},
            "candidate": {"guid": SON, "title": SON_TITLE, "size": 255225091}, **fields}


def row(minutes: int, kind: str, **data) -> tuple:
    return AT + timedelta(minutes=minutes), kind, data


def grab(minutes: int, guid: str, title: str, size: int) -> tuple:
    return row(minutes, "grab", guid=guid, release=title, size=size)


ROWS = [grab(0, WALKMAN, WALKMAN_TITLE, 420043632), row(1, "failure", reason=ENCRYPTED, attempts=1),
        grab(2, JAPANESE, JAPANESE_TITLE, 49799414), row(3, "failure", reason=REPAIR, attempts=2),
        grab(4, SON, SON_TITLE, 255225091)]
# The same grabs refused for what they held: the two a reset takes off.
JUDGED = [grab(0, WALKMAN, WALKMAN_TITLE, 420043632), row(1, "failure", reason=WRONG, attempts=1),
          grab(2, JAPANESE, JAPANESE_TITLE, 49799414), row(3, "import_incomplete", release=JAPANESE_TITLE, missing=2),
          grab(4, SON, SON_TITLE, 255225091)]


def test_a_failure_with_no_guid_is_the_grab_before_it():
    walkman, japanese = blocklist.entries(nilsson(), ROWS)
    assert walkman == {"guid": WALKMAN, "title": WALKMAN_TITLE, "size": 420043632, "reason": ENCRYPTED,
                       "at": "2026-11-03T09:53:00Z", "kind": blocklist.DEAD_DOWNLOAD, "every_copy": False}
    assert (japanese["title"], japanese["reason"]) == (JAPANESE_TITLE, REPAIR)


def test_a_failure_with_its_guid_needs_no_order():
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 1), grab(1, JAPANESE, JAPANESE_TITLE, 2),
            row(2, "failure", reason=ENCRYPTED, guid=WALKMAN), row(3, "failure", reason=REPAIR, guid=JAPANESE)]
    assert [e["reason"] for e in blocklist.entries(nilsson(), rows)] == [ENCRYPTED, REPAIR]


def test_a_refused_grab_has_no_grab_event_and_names_itself():
    rows = [row(0, "failure", reason="404", release=WALKMAN_TITLE, guid=WALKMAN, what="grab")]
    (entry, _) = blocklist.entries(nilsson(), rows)
    assert (entry["title"], entry["reason"]) == (WALKMAN_TITLE, "404")


def test_a_refused_grab_from_before_the_guid_is_not_the_grab_before_it():
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 1), row(1, "failure", reason="404", release=JAPANESE_TITLE, what="grab")]
    assert [e["reason"] for e in blocklist.entries(nilsson(), rows)] == ["unknown", "unknown"]


def test_an_incomplete_copy_says_what_it_lacks():
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 1), row(1, "import_incomplete", release=WALKMAN_TITLE, missing=2)]
    assert blocklist.entries(nilsson(), rows)[0]["reason"] == "lacks 2 track(s) of its own release"


def test_a_wrong_album_is_the_failure_after_it():
    """complete() records the [WRONG]'s reason on the failure it falls through to."""
    reason = "another album: 0 of the album's 5 titles in it"
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 1), row(1, "failure", reason=reason, gave_up=True)]
    assert blocklist.entries(nilsson(), rows)[0]["reason"] == reason


def test_only_the_first_outcome_after_a_grab_is_its_own():
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 1), row(1, "failure", reason=ENCRYPTED),
            row(2, "failure", reason=REPAIR)]
    assert blocklist.entries(nilsson(), rows)[0]["reason"] == ENCRYPTED


def test_the_latest_outcome_of_a_release_grabbed_twice_stands():
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 1), row(1, "failure", reason=ENCRYPTED),
            grab(2, WALKMAN, WALKMAN_TITLE, 1), row(3, "failure", reason=REPAIR)]
    assert blocklist.entries(nilsson(), rows)[0]["reason"] == REPAIR


def test_an_entry_older_than_the_events_is_unknown():
    assert blocklist.entries(nilsson(blocklist=["https://indexer/details/old"]), ROWS) == [
        {"guid": "https://indexer/details/old", "title": None, "size": None, "reason": "unknown", "at": None,
         "kind": None, "every_copy": False}]


def test_the_records_own_copies_name_themselves():
    record = nilsson(blocklist=[SON, WALKMAN], best_copy={"guid": WALKMAN, "title": WALKMAN_TITLE, "missing": 1})
    son, walkman = blocklist.entries(record, [])
    assert (son["title"], son["size"], son["reason"]) == (SON_TITLE, 255225091, "unknown")
    assert (walkman["title"], walkman["reason"]) == (WALKMAN_TITLE, "unknown")


def test_an_unreadable_store_says_so():
    assert blocklist.entries(nilsson(), None)[0]["reason"] == blocklist.UNREADABLE


def record_events(rows: list[tuple] = ROWS) -> None:
    for at, kind, data in rows:
        reason = data.get("reason") or ""
        assert events.record(kind, f"[{kind}] {reason}", source="albums", spotify_album_id=SID,
                             data={"key": "1274", **data})
    # spotdl fills a fallback under the same Spotify album: never a release's reason.
    assert events.record("failure", "[MISS] a track", source="spotdl", spotify_album_id=SID,
                         data={"reason": "LookupError: No results found"})


@needs_pg
def test_blocklist_entries_read_the_event_store():
    record_events()
    assert [(e["title"], e["reason"]) for e in albums.blocklist_entries("1274", nilsson())] == [
        (WALKMAN_TITLE, ENCRYPTED), (JAPANESE_TITLE, REPAIR)]


def run_reset(monkeypatch, caplog, *flags: str) -> list[str]:
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["music-album-reset", "Harry Nilsson — Nilsson Schmilsson", *flags])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_fetch.albums"):
        albums.reset_main()
    return [r.getMessage() for r in caplog.records if r.name == "music_fetch.albums"]


def seed_nilsson(rows: list[tuple] = ROWS, **fields) -> None:
    State(albums={"1274": nilsson(**fields)}).save()
    record_events(rows)


def stored() -> dict:
    (record,) = State.load().albums.values()  # saved under a key of its own, not "1274"
    return record


def test_a_reimport_reset_lists_what_it_frees_and_what_that_costs(monkeypatch, caplog):
    seed_nilsson(JUDGED)
    lines = run_reset(monkeypatch, caplog, "--reimport")
    assert "Clears from the blocklist (2 of 2):" in lines
    assert (f"  {WALKMAN_TITLE} (420 MB): {WRONG}{EVERY_WRONG}; the matcher accepts it again") in lines
    assert (f"  {JAPANESE_TITLE} (50 MB): {LACKS}{EVERY_WRONG}; the matcher accepts it again") in lines
    assert ("The tick may grab 2 cleared release(s) again. Each that fails the same way costs an attempt: "
            "up to 2 of the 3 the album then has.") in lines
    assert any(line.startswith("The next tick with a free slot grabs the best release") for line in lines)
    assert OLD_ITEMS in lines
    assert lines == run_reset(monkeypatch, caplog, "--reimport")


def test_a_plain_reset_clears_only_the_last_candidate(monkeypatch, caplog):
    """1274's last candidate imported, so a plain reset clears nothing at all."""
    seed_nilsson()
    lines = run_reset(monkeypatch, caplog)
    assert "Clears nothing from the blocklist" in lines
    assert STAYS in lines
    assert f"  {WALKMAN_TITLE} (420 MB): {ENCRYPTED}{ONE_DEAD}" in lines
    assert not any(line.startswith("The tick may grab") for line in lines)
    assert any("turns have and nothing is grabbed" in line for line in lines)
    assert PLAIN_OLD_ITEMS in lines


def test_a_plain_reset_of_a_blocklisted_candidate_frees_that_one(monkeypatch, caplog):
    seed_nilsson(JUDGED, candidate={"guid": JAPANESE, "title": JAPANESE_TITLE, "size": 49799414}, status=albums.FAILED)
    lines = run_reset(monkeypatch, caplog)
    assert "Clears from the blocklist (1 of 2):" in lines
    assert f"  {JAPANESE_TITLE} (50 MB): {LACKS}{EVERY_WRONG}; the matcher accepts it again" in lines
    assert f"  {WALKMAN_TITLE} (420 MB): {WRONG}{EVERY_WRONG}" in lines
    assert any("up to 1 of the 2 the album then has" in line for line in lines)


def test_a_reimport_keeps_a_release_that_failed_as_a_download(monkeypatch, caplog):
    """Album 1454: two uploads failed repair.  Cleared, they could be grabbed again and fail the same
    way, an attempt each: better no album than a broken one (homelab#2661)."""
    seed_nilsson()
    lines = run_reset(monkeypatch, caplog, "--reimport", "--apply")
    assert "Clears nothing from the blocklist" in lines and STAYS in lines
    assert f"  {WALKMAN_TITLE} (420 MB): {ENCRYPTED}{ONE_DEAD}" in lines
    assert f"  {JAPANESE_TITLE} (50 MB): {REPAIR}{ONE_DEAD}" in lines
    assert not any(line.startswith("The tick may grab") for line in lines)
    record = stored()
    assert record["blocklist"] == [WALKMAN, JAPANESE]
    assert record["attempts"] == 0 and record["reimport"] is True and record["status"] == albums.WANTED


def test_a_plain_reset_keeps_a_last_candidate_that_failed_as_a_download(monkeypatch, caplog):
    seed_nilsson(candidate={"guid": JAPANESE, "title": JAPANESE_TITLE, "size": 49799414}, status=albums.FAILED,
                 attempts=3)
    lines = run_reset(monkeypatch, caplog, "--apply")
    assert "Clears nothing from the blocklist" in lines
    record = stored()
    assert record["blocklist"] == [WALKMAN, JAPANESE] and record["attempts"] == 2


def test_a_reimport_clears_what_was_judged_and_keeps_the_rest_in_order(monkeypatch, caplog):
    rows = [grab(0, JAPANESE, JAPANESE_TITLE, 49799414), row(1, "failure", reason=WRONG),
            grab(2, WALKMAN, WALKMAN_TITLE, 420043632), row(3, "failure", reason=REPAIR)]
    other = "https://indexer/details/other"
    seed_nilsson(rows, blocklist=[other, JAPANESE, WALKMAN], candidate={})
    lines = run_reset(monkeypatch, caplog, "--reimport", "--apply")
    assert "Clears from the blocklist (1 of 3):" in lines
    assert f"  {JAPANESE_TITLE} (50 MB): {WRONG}{EVERY_WRONG}; the matcher accepts it again" in lines
    assert stored()["blocklist"] == [other, WALKMAN]


def test_an_import_of_nothing_stays_like_a_broken_download(monkeypatch, caplog):
    """Album 1550: a job beets could import none of was no verdict on its music."""
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 420043632), row(1, "failure", reason="beets imported none of its files")]
    seed_nilsson(rows, blocklist=[WALKMAN])
    run_reset(monkeypatch, caplog, "--reimport", "--apply")
    assert stored()["blocklist"] == [WALKMAN]


def test_an_unreadable_event_store_clears_nothing(monkeypatch, caplog):
    seed_nilsson(JUDGED)
    monkeypatch.setattr(blocklist, "album_events", lambda *_: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    lines = run_reset(monkeypatch, caplog, "--reimport", "--apply")
    assert "Clears nothing from the blocklist" in lines
    assert stored()["blocklist"] == [WALKMAN, JAPANESE]


@pytest.mark.parametrize(("reason", "stays"), [
    (WRONG, False), (LACKS, False), (ENCRYPTED, True), (REPAIR, True), ("download failed", True), ("404", True),
    ("beets imported none of its files", True), ("the import crashed 3 times", True), (blocklist.UNKNOWN, True),
    (blocklist.UNREADABLE, True)])
def test_only_a_verdict_on_what_a_release_held_leaves_the_blocklist(reason, stays):
    assert blocklist.stays({"reason": reason}) is stays


# ---------------------------------------------------------------------------
# A wrong release or a dead download: what an entry blocks (homelab#2757)
# ---------------------------------------------------------------------------


def nest(title: str = WALKMAN_TITLE, guid: str = "https://nzbnest/details/other") -> Release:
    return Release(guid=guid, title=title, size=420043632, indexer_id=2, download_url="d", categories=[])


@pytest.mark.parametrize(("reason", "kind"), [
    (WRONG, blocklist.WRONG_RELEASE), (LACKS, blocklist.WRONG_RELEASE), (ENCRYPTED, blocklist.DEAD_DOWNLOAD),
    (REPAIR, blocklist.DEAD_DOWNLOAD), ("download failed", blocklist.DEAD_DOWNLOAD), ("404", blocklist.DEAD_DOWNLOAD),
    ("beets imported none of its files", blocklist.DEAD_DOWNLOAD), (blocklist.UNKNOWN, None),
    (blocklist.UNREADABLE, None)])
def test_an_entry_is_a_wrong_release_or_a_dead_download(reason, kind):
    assert blocklist.kind({"reason": reason}) == kind
    # A reset takes off a wrong release only.
    assert blocklist.stays({"reason": reason}) is (kind != blocklist.WRONG_RELEASE)


def test_a_wrong_release_blocks_every_copy_of_its_title():
    rows = [grab(0, WALKMAN, WALKMAN_TITLE, 420043632), row(1, "failure", reason=WRONG)]
    (entry,) = listed = blocklist.entries(nilsson(blocklist=[WALKMAN]), rows)
    assert entry["kind"] == blocklist.WRONG_RELEASE and entry["every_copy"]
    copies = blocklist.blocked(listed)
    assert nest() in copies and nest(f"{WALKMAN_TITLE}-1") in copies
    assert nest(JAPANESE_TITLE) not in copies


def test_a_wrong_release_with_no_title_blocks_only_its_guid():
    (entry,) = listed = blocklist.entries(nilsson(blocklist=[WALKMAN], candidate={}),
                                          [row(0, "failure", reason=WRONG, guid=WALKMAN)])
    assert entry["kind"] == blocklist.WRONG_RELEASE and not entry["every_copy"]
    assert blocklist.blocked(listed).guids == {WALKMAN} and not blocklist.blocked(listed).names


def test_a_dead_download_blocks_only_its_guid_until_a_second_copy_dies():
    (entry,) = listed = blocklist.entries(nilsson(blocklist=[WALKMAN]), ROWS[:2])
    assert entry["kind"] == blocklist.DEAD_DOWNLOAD and not entry["every_copy"]
    copies = blocklist.blocked(listed)
    assert nest(guid=WALKMAN) in copies and nest(guid=NEST) not in copies

    # NZBNest's copy, tried once, died too: the title is dead on Usenet.
    rows = [*ROWS[:2], grab(5, NEST, WALKMAN_TITLE, 420043632), row(6, "failure", reason=REPAIR, guid=NEST)]
    listed = blocklist.entries(nilsson(blocklist=[WALKMAN, NEST]), rows)
    assert [(e["kind"], e["every_copy"]) for e in listed] == [(blocklist.DEAD_DOWNLOAD, True)] * 2
    assert nest(f"{WALKMAN_TITLE}-1") in blocklist.blocked(listed)


def test_a_reset_dry_run_says_what_each_entry_blocks(monkeypatch, caplog):
    """WALKMAN died on both indexers; JAPANESE was incomplete; a repost of WALKMAN another album."""
    repost = "https://indexer/details/walkman-1"
    rows = [*ROWS[:2], grab(5, NEST, WALKMAN_TITLE, 420043632), row(6, "failure", reason=REPAIR, guid=NEST),
            grab(7, JAPANESE, JAPANESE_TITLE, 49799414), row(8, "import_incomplete", release=JAPANESE_TITLE, missing=2),
            grab(9, repost, f"{WALKMAN_TITLE}-1", 420043632), row(10, "failure", reason=WRONG, guid=repost)]
    seed_nilsson(rows, blocklist=[WALKMAN, NEST, JAPANESE, repost], candidate={})
    lines = run_reset(monkeypatch, caplog, "--reimport")
    assert "Clears from the blocklist (2 of 4):" in lines
    assert f"  {JAPANESE_TITLE} (50 MB): {LACKS}{EVERY_WRONG}; the matcher accepts it again" in lines
    assert (f"  {WALKMAN_TITLE}-1 (420 MB): {WRONG}{EVERY_WRONG}; the matcher accepts it, but an entry that stays "
            "blocks every copy of it, so it isn't grabbed") in lines
    twice = " (a dead download, a second dead copy of its title, so every copy of it)"
    assert f"  {WALKMAN_TITLE} (420 MB): {ENCRYPTED}{twice}" in lines
    assert f"  {WALKMAN_TITLE} (420 MB): {REPAIR}{twice}" in lines
    assert any("up to 1 of the 3 the album then has" in line for line in lines)
    assert lines == run_reset(monkeypatch, caplog, "--reimport")


def test_a_cleared_release_the_matcher_refuses_is_not_a_cost(monkeypatch, caplog):
    other = "https://indexer/details/other"
    seed_nilsson(blocklist=[other])
    events.record("grab", "[GRAB]", source="albums", spotify_album_id=SID,
                  data={"guid": other, "release": SON_TITLE, "size": 255225091})
    events.record("failure", "[FAIL]", source="albums", spotify_album_id=SID, data={"reason": WRONG})
    lines = run_reset(monkeypatch, caplog, "--reimport")
    assert (f"  {SON_TITLE} (255 MB): {WRONG}{EVERY_WRONG}; "
            "the matcher refuses it (artist words: nilsson, beside son nmr)") in lines
    assert not any(line.startswith("The tick may grab") for line in lines)


def test_a_reset_judges_a_release_as_the_override_would(monkeypatch, caplog):
    seed_nilsson(JUDGED)
    with state.connect() as conn:
        state.put(conn, state.OVERRIDES_IMPORTED, {"at": "now"})
    overrides_cli.main(["search", "--apply", "--", "Harry Nilsson — Nilsson Schmilsson", "Nilsson Schmilsson"])
    lines = run_reset(monkeypatch, caplog, "--reimport")
    assert any(WALKMAN_TITLE in line and "the matcher refuses it (artist: doesn't start with an album word)" in line
               for line in lines)
    assert not any(line.startswith("The tick may grab") for line in lines)


def test_an_entry_with_no_grab_recorded_cant_be_judged(monkeypatch, caplog):
    old = "https://indexer/details/old"
    seed_nilsson([row(0, "failure", reason=WRONG, guid=old)], blocklist=[old], candidate={})
    lines = run_reset(monkeypatch, caplog, "--reimport")
    assert f"  {old}: {WRONG}{UNTITLED_WRONG}; the matcher can't be asked: its title or size isn't recorded" in lines
    assert not any(line.startswith("The tick may grab") for line in lines)


def test_a_release_another_album_holds_is_not_grabbed(monkeypatch, caplog):
    State(albums={"1274": nilsson(),
                  "9": {"artist": "Harry Nilsson", "name": "Other", "status": albums.IMPORTED, "blocklist": [],
                        "spotify_album_ids": ["0ETFjACtuP2ADo6LFhL6HN"], "playlists": {},
                        "candidate": {"guid": WALKMAN, "title": WALKMAN_TITLE}}}).save()
    record_events(JUDGED)
    lines = run_reset(monkeypatch, caplog, "--reimport")
    assert any(WALKMAN_TITLE in line and "another album holds it" in line for line in lines)
    assert any("up to 1 of the 3 the album then has" in line for line in lines)


# ---------------------------------------------------------------------------
# set_search_override
# ---------------------------------------------------------------------------


def release(title: str, size: int = 255225091) -> Release:
    return Release(guid="g", title=title, size=size, indexer_id=0, download_url="", categories=[])


def judged(title: str, words: str, artist: str = "", album: str = "", tracks: int = 10):
    """The matcher on *title*: under an override of *words* when no artist is given, as ``plan()`` calls it."""
    why = rejection(release(title), artist, album or words, tracks, album_type="album", seconds=2815)
    return str(why) if why else None


def test_what_the_dry_run_says_an_override_drops_is_what_the_matcher_drops():
    # The artist check becomes "starts with one of the words".
    assert judged(SON_TITLE, "nilsson schmilsson") == "artist: doesn't start with an album word"
    assert judged(SON_TITLE, "harry nilsson schmilsson") is None
    # Self-titled: the name once, beside other words, is refused only with the artist known.
    assert judged("Marci-Some Words-FLAC-2020-GRP", "marci", "Marci", "Marci").startswith("self-titled")
    assert judged("Marci-Some Words-FLAC-2020-GRP", "marci") is None
    # Artist words.
    title = "Curtis Mayfield-Live Europe-FLAC-1972-GRP"
    assert judged(title, "", "Curtis Mayfield", "Curtis Live!").startswith("artist words")
    assert judged(title, "curtis live") is None


def test_what_the_dry_run_says_stays_is_what_the_matcher_keeps():
    assert judged("Nilsson Schmilsson Tribute-FLAC-2020-GRP", "nilsson schmilsson") == "reject word: tribute"
    assert judged("Nilsson Schmilsson One Two Three-FLAC-2020-GRP", "nilsson schmilsson").startswith("leftover words")
    assert judged("Nilsson Schmilsson Tour-FLAC-2020-GRP", "nilsson schmilsson") == "other release: tour"
    assert judged("Nilsson Schmilsson EP-FLAC-2020-GRP", "nilsson schmilsson").startswith("single")
    assert rejection(release("Nilsson Schmilsson-FLAC-2020-GRP", 1), "", "nilsson schmilsson", 10,
                     seconds=2815).rule == "size"
    # The artist's words the override lacks are leftovers.
    assert judged("Harry Lee Nilsson Schmilsson-FLAC-GRP", "lee schmilsson") == "artist: doesn't start with an album word"
    assert judged("Harry Lee Nilsson Jr Schmilsson-FLAC-2020-GRP", "harry schmilsson").startswith("leftover words")


def test_every_word_must_be_in_the_title_in_any_order():
    assert judged("Curtis Mayfield-Curtis Live-FLAC-GRP", "curtis live 1971") == "album words: lacks 1971"
    assert judged("Schmilsson Nilsson-FLAC-2020-GRP", "nilsson schmilsson") is None
    assert judged("Nilsson Schmilsson-FLAC-2020-GRP", "the nilsson schmilsson") is None
    assert judged(WALKMAN_TITLE, "nilsson schmilsson son").startswith("album words: lacks son")


def test_a_dashed_word_is_refused_and_a_dash_in_a_name_is_not():
    assert exclusion('Nilsson Schmilsson -Son').startswith("-Son: there is no exclusion syntax")
    assert exclusion('"Nilsson Schmilsson" "-Son"').startswith("-Son:")
    assert exclusion("--Son 1971") and exclusion("-1971")
    assert exclusion("Jay-Z Reasonable Doubt - Remastered") == ""


def test_leading_word_skips_articles():
    assert leading_word("The Beatles") == "beatles"
    assert leading_word("〠") == ""


@pytest.fixture
def imported_overrides():
    State(albums={"1274": nilsson()}).save()
    with state.connect() as conn:
        state.put(conn, state.OVERRIDES_IMPORTED, {"at": "now"})


def override_lines(caplog, words: list[str]) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        overrides_cli.main(["search", "--", "Nilsson Schmilsson", *words])
    return [r.getMessage() for r in caplog.records]


def test_the_override_dry_run_says_what_it_changes(caplog, imported_overrides):
    lines = override_lines(caplog, ["Nilsson", "Schmilsson"])
    assert any(line.startswith("Every word must be in a release title") and "needs 1971 in the title" in line
               for line in lines)
    assert any(line.startswith("Dropped with the artist:") and "self-titled" in line and "artist words" in line
               for line in lines)
    assert any(line.startswith("Still applied:") and "at most 2" in line for line in lines)
    assert ("harry isn't among these words, so a title starting with the artist's name is refused "
            "(artist: doesn't start with an album word). Add it to match one.") in lines
    assert lines == override_lines(caplog, ["Nilsson", "Schmilsson"])


def test_the_override_dry_run_doesnt_warn_when_the_artist_leads(caplog, imported_overrides):
    assert not any("isn't among these words" in line for line in override_lines(caplog, ["Harry", "Schmilsson"]))


def test_clearing_an_override_prints_no_matcher_notes(caplog, imported_overrides):
    with caplog.at_level(logging.INFO):
        overrides_cli.main(["clear-search", "--", "Nilsson Schmilsson"])
    assert "Every word must" not in caplog.text


def test_the_cli_refuses_a_dashed_word(caplog, imported_overrides):
    with pytest.raises(SystemExit):
        overrides_cli.main(["search", "--", "Nilsson Schmilsson", "Nilsson", "Schmilsson", "-Son"])
    assert "there is no exclusion syntax" in caplog.text

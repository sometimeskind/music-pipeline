"""music-album-audit-releases --retire: wrong local-only extras off their album (homelab#2648)."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

import psycopg
import pytest

from music_scan import release
from music_scan.identity import Recordings

# fetch/tests in the repo (CI), /app/fetch_tests in the dev image (just test).
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(next((p for p in (_here.parent.parent / "fetch" / "tests", _here.parent / "fetch_tests")
                             if (p / "pg_fixture.py").exists()), _here)))
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

ALBUM = "0PMasrHdpaoIRuHuhHp72O"
RECORDS = {"375": {"artist": "Bruce Springsteen", "name": "Born In The U.S.A.", "status": "imported",
                   "spotify_album_ids": [ALBUM]}}
GROUP = "3c39a076-8e52-38f2-8076-260d0672fb23"


class Item(dict):
    def __init__(self, id_, dest="/elsewhere.m4a", **fields):
        super().__init__(**fields)
        self.id, self.dest, self.removed = id_, dest, False

    def get(self, key, default=None):
        return super().get(key, default)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self.get(name)

    def remove(self, delete=False):
        assert delete is False
        self.removed = True

    def destination(self):
        return os.fsencode(self.dest)


@pytest.fixture
def library(tmp_path):
    """Album 375 as 2026-11-04 had it: a release item, three live extras, a spotdl copy."""
    folder = tmp_path / "library"
    folder.mkdir()

    def item(id_, name, **fields):
        (folder / name).write_bytes(b"audio")
        return Item(id_, path=str(folder / name), title=name, artist="Bruce Springsteen",
                    album="Born In The U.S.A.", length=371.4, **fields)

    extra = {"album_record": "375", "album_extra": "1", "via": "usenet"}
    return {
        18520: item(18520, "stray.m4a", album_record="375", via="usenet", mb_releasegroupid=GROUP, canon_clash="1"),
        8283: item(8283, "06 - live.m4a", **extra),
        8276: item(8276, "11 - live.m4a", isrc="QM9WB1400988", **extra),
        8277: item(8277, "12 - live.m4a", isrc="QM9WB1400988", **extra),
        8275: item(8275, "08 - Bobby Jean.m4a", spotify_ids="2KPzDsCtQtIM0Lkh3RacRC", **extra),
        534: item(534, "09 - spotdl.m4a", via="spotdl", spotify_album_id=ALBUM,
                  spotify_url="https://open.spotify.com/track/0VkDedVtP7TQDlaCMq9QVx"),
    }


def dry(library, ids, songs=None, local=None, records=None, tmp=Path("/nonexistent")):
    return release.retire_chosen(list(library.values()), ids, "the wrong box set", records or RECORDS, songs or {},
                                 local or {}, tmp)


def entry(song_id=None, isrc=None, name="Bobby Jean"):
    return {"song_id": song_id, "isrc": isrc, "name": name, "artists": ["Bruce Springsteen"]}


def test_a_dry_run_moves_nothing_and_says_what_it_would_do(library, caplog):
    caplog.set_level("INFO")
    assert dry(library, [8276, 8283]) == []
    assert not any(i.removed for i in library.values())
    assert all(Path(i["path"]).exists() for i in library.values())
    assert "[RETIRE] 8283 Bruce Springsteen — 06 - live.m4a [Born In The U.S.A.] (371s): →" in caplog.text
    assert "Reason: the wrong box set" in caplog.text and "Dry run — nothing retired" in caplog.text


def test_two_dry_runs_print_the_same(library, caplog):
    """The gate re-runs the dry run and compares its output: nothing in it may drift."""
    caplog.set_level("INFO")
    songs = {"happy": [entry(isrc="QM9WB1400988")], "cool": [entry(isrc="QM9WB1400988")]}
    library[1] = Item(1, path="/l/other.m4a", isrc="QM9WB1400988", album_record="9")

    def run():
        caplog.clear()
        dry(library, [8276, 8283], songs)
        return [r.getMessage() for r in caplog.records]

    first = run()
    assert first == run()
    assert first.index(next(line for line in first if "cool:" in line)) < first.index(
        next(line for line in first if "happy:" in line))  # playlists in order, not as the store gave them


def test_an_unknown_item_is_refused(library):
    with pytest.raises(SystemExit, match="no library item with id 99; nothing moved"):
        dry(library, [8283, 99])


def refused(library, caplog, ids, **kw):
    caplog.set_level("INFO")
    with pytest.raises(SystemExit, match=r"refused; nothing moved"):
        dry(library, ids, **kw)
    assert not any(i.removed for i in library.values())
    return caplog.text


def test_a_release_item_is_refused(library, caplog):
    assert "[REFUSED] 18520" in (text := refused(library, caplog, [18520]))
    assert "a release item of album 375: the album is the unit" in text


def test_a_loose_spotdl_copy_is_no_extra(library, caplog):
    """534: on the album's Spotify album by canon, but no album_extra and no album_record."""
    assert "[REFUSED] 534" in (text := refused(library, caplog, [534]))
    assert "not a local-only extra" in text


def test_a_hand_imported_extra_is_refused(library, caplog):
    library[8283]["via"] = "manual"
    assert "imported by hand (via=manual)" in refused(library, caplog, [8283])


def test_an_extra_with_no_record_is_refused(library, caplog):
    library[8283]["album_record"] = "999"
    assert "its album record 999 doesn't exist" in refused(library, caplog, [8283])


def test_an_extra_of_an_album_mid_import_is_refused(library, caplog):
    records = {"375": {**RECORDS["375"], "status": "grabbed"}}
    assert "album 375 is mid-import" in refused(library, caplog, [8283], records=records)


def test_an_extra_of_an_album_with_no_release_is_refused(library, caplog):
    del library[18520]
    assert "album 375 has no release items" in refused(library, caplog, [8283])


def test_an_extra_whose_file_is_gone_is_refused(library, caplog):
    Path(library[8283]["path"]).unlink()
    assert "is gone" in refused(library, caplog, [8283])


def test_the_last_item_an_entry_resolves_to_is_refused(library, caplog):
    """8275: fancy's entry holds it by Spotify ID, and the release's Bobby Jean holds nothing."""
    songs = {"fancy": [entry("2KPzDsCtQtIM0Lkh3RacRC")]}
    text = refused(library, caplog, [8275], songs=songs)
    assert "[REFUSED] 8275" in text and "no other item would take the slot" in text
    assert "fancy: Bobby Jean — Bruce Springsteen (Spotify ID 2KPzDsCtQtIM0Lkh3RacRC, ISRC none) → no item" in text


def test_one_refusal_refuses_the_whole_call(library, caplog):
    text = refused(library, caplog, [8275, 8283], songs={"fancy": [entry("2KPzDsCtQtIM0Lkh3RacRC")]})
    assert "[RETIRE] 8283" in text and "[REFUSED] 8275" in text
    assert Path(library[8283]["path"]).exists()


def test_an_entry_another_item_takes_passes(library, caplog):
    caplog.set_level("INFO")
    library[18520]["spotify_ids"] = "2KPzDsCtQtIM0Lkh3RacRC"
    dry(library, [8275], {"fancy": [entry("2KPzDsCtQtIM0Lkh3RacRC")]})
    assert "→ 18520 by Spotify ID" in caplog.text


def test_an_entry_held_by_isrc_passes_to_the_item_holding_it(library, caplog):
    caplog.set_level("INFO")
    dry(library, [8276], {"cool": [entry(isrc="QM9WB1400988")]})
    assert "→ 8277 by ISRC" in caplog.text


def test_two_chosen_items_can_not_take_each_others_slot(library, caplog):
    assert "no other item would take the slot" in refused(library, caplog, [8276, 8277],
                                                          songs={"cool": [entry(isrc="QM9WB1400988")]})


def test_a_local_entry_naming_the_item_is_refused(library, caplog):
    assert "a local entry of cool names it" in refused(library, caplog, [8283], local={8283: ["cool"]})


def test_the_dry_run_names_the_release_item_a_retire_frees(library, caplog):
    caplog.set_level("INFO")
    library[18520].dest = library[8275]["path"]
    dry(library, [8275])
    assert "[FREED] 18520 stray.m4a waits for the path 8275 holds; the next scan's canon moves it there" in caplog.text


def test_a_clash_on_another_path_is_not_freed(library, caplog):
    caplog.set_level("INFO")
    library[18520].dest = "/elsewhere.m4a"
    dry(library, [8275])
    assert "[FREED]" not in caplog.text


def _events(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT kind, source, spotify_album_id, release_group::text, item_id, caller, message, "
                            "data FROM events ORDER BY id").fetchall()


@needs_pg
def test_a_clean_retire_moves_the_files_and_records_each(library, tmp_path, pg_url, monkeypatch):
    from music_fetch import db

    monkeypatch.setenv("MUSIC_EVENT_CALLER", "someone")
    replaced = tmp_path / "replaced"
    path = library[8283]["path"]
    with contextlib.closing(db.connect()) as conn:
        moved = release.retire_chosen(list(library.values()), [8276, 8283], "the wrong box set", RECORDS, {}, {},
                                      replaced, conn)
    assert moved == [replaced / "retired" / "8276-11 - live.m4a", replaced / "retired" / "8283-06 - live.m4a"]
    assert all(f.read_bytes() == b"audio" for f in moved) and not Path(path).exists()
    assert library[8283].removed and library[8276].removed and not library[8277].removed
    first, second = _events(pg_url)
    assert first[:6] == ("retire", "release", ALBUM, GROUP, 8276, "someone")
    kind, _, _, _, item_id, _, message, data = second
    assert (kind, item_id) == ("retire", 8283)
    assert message == ("[RETIRE] 8283 Bruce Springsteen — 06 - live.m4a [Born In The U.S.A.] (chosen): "
                       "the wrong box set")
    assert data == {"artist": "Bruce Springsteen", "title": "06 - live.m4a", "album": "Born In The U.S.A.",
                    "path": path, "origin": "chosen", "reason": "the wrong box set"}


@needs_pg
def test_a_failed_move_records_nothing(library, tmp_path, pg_url, monkeypatch):
    from music_fetch import db
    from music_scan import rollback

    def fail(*_):
        raise OSError("read-only file system")

    monkeypatch.setattr(rollback.shutil, "move", fail)
    with contextlib.closing(db.connect()) as conn, pytest.raises(OSError):
        release.retire_chosen(list(library.values()), [8283], "why", RECORDS, {}, {}, tmp_path / "replaced", conn)
    assert _events(pg_url) == [] and not library[8283].removed


@pytest.fixture
def replaced(tmp_path):
    folder = tmp_path / "replaced"
    (folder / release.RETIRED).mkdir(parents=True)
    for name in ("retired/8283-06 - live.m4a", "17052-other.m4a"):
        (folder / name).write_bytes(b"not audio: no readable tags")
    return folder


def test_a_retired_file_is_superseded_and_no_only_copy(replaced):
    """Nothing in the library holds its recording, and its tags are unreadable: someone chose to drop it."""
    held, only = release.split_replaced(release.audio_files(replaced), Recordings())
    assert held == [replaced / "retired" / "8283-06 - live.m4a"]
    assert only == [replaced / "17052-other.m4a"]  # the other rule stands: nothing says the library has it


def test_the_purge_deletes_a_retired_file_after_the_retention(replaced, caplog, monkeypatch):
    recorded = []
    monkeypatch.setattr(release.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    caplog.set_level("INFO")
    f = replaced / "retired" / "8283-06 - live.m4a"
    assert release.purge_replaced(replaced, Recordings(), 14) == {"superseded": 1, "only_copy": 1, "purged": 0}
    assert f.exists()
    counts = release.purge_replaced(replaced, Recordings(), 14, now=time.time() + 15 * 86400)
    assert counts == {"superseded": 0, "only_copy": 1, "purged": 1}
    assert not f.exists() and (replaced / "17052-other.m4a").exists()
    [(kind, message, kw)] = recorded
    assert (kind, kw["item_id"]) == ("purged", 8283)
    assert message == f"[PURGE] {f}: retired by hand 14+ days ago"


def test_keep_rescues_a_retired_file_by_its_item_id(replaced, tmp_path):
    assert release.keep_target("8283", replaced) == replaced / "retired" / "8283-06 - live.m4a"
    assert release.keep_target("retired/8283-06 - live.m4a", replaced).exists()


def test_freed_reads_a_real_items_destination(tmp_path, caplog):
    """A beets item's destination, as canon's clash check reads it: bytes, from the configured path format."""
    from beets import config
    from beets.library import Item as BeetsItem

    from music_scan.library import MusicLibrary

    config["paths"].set({"default": "$albumartist/$album/$track - $title",
                         "singleton": "$albumartist/$album/$track - $title"})
    root = tmp_path / "library"
    held = root / "Artist" / "Album" / "08 - Song.m4a"
    tags = {"albumartist": "Artist", "album": "Album", "title": "Song", "track": 8}
    with MusicLibrary(tmp_path / "library.db", root) as lib:
        extra = BeetsItem(path=os.fsencode(held), **tags)
        clash = BeetsItem(path=os.fsencode(root / "Artist" / "08 - Song.m4a"), **tags)
        lib._lib.add(extra)
        lib._lib.add(clash)
        clash["canon_clash"] = "1"
        clash.store()
        lines = release._freed([extra], [extra, clash])
    assert lines == [f"[FREED] {clash.id} Song waits for the path {extra.id} holds; the next scan's canon moves it "
                     "there"]

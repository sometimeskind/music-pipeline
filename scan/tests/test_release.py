"""A Usenet release imported as one album (#279)."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_scan import release


class FakeItem(dict):
    def __init__(self, id_=1, added=100.0, **fields):
        super().__init__(**fields)
        self.id, self.added, self.stored = id_, added, 0

    def get(self, key, default=None):
        return super().get(key, default)

    def __getattr__(self, name):
        # A beets Item reads its fields as attributes too (music_scan.dupes does).
        if name.startswith("__"):
            raise AttributeError(name)
        return self.get(name)

    def store(self):
        self.stored += 1


@pytest.fixture
def ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")


def _m4a(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)
    if tags:
        from mediafile import MediaFile

        mf = MediaFile(str(path))
        for k, v in tags.items():
            setattr(mf, k, v)
        mf.save()
    return path


def _match(distance, album_id="MB1", extra_tracks=0, extra_items=0, tracks=10):
    info = SimpleNamespace(album_id=album_id, album="Ready to Die", tracks=[object()] * tracks,
                           releasegroup_id="RG1", artist_id="A1", artists_ids=["A1"], albumdisambig="",
                           releasegroupdisambig="")
    return SimpleNamespace(distance=distance, info=info, extra_tracks=[object()] * extra_tracks,
                           extra_items=[object()] * extra_items)


def test_match_folder_reports_the_closest_release_and_its_missing_tracks(tmp_path, ffmpeg) -> None:
    files = [_m4a(tmp_path / f"{n:02d}.m4a") for n in (1, 2)]
    proposal = SimpleNamespace(candidates=[_match(0.3, "FAR"), _match(0.05, "CD94", extra_tracks=1, extra_items=1)])
    found = release.match_folder(files, tag_album=lambda source: proposal)
    assert found["mb_albumid"] == "CD94" and found["missing"] == 1 and found["unmatched"] == 1
    assert found["files"] == 2 and found["mb"] == {"mb_albumid": "CD94", "mb_releasegroupid": "RG1",
                                                   "mb_albumartistid": "A1", "mb_albumartistids": ["A1"]}


def test_match_folder_with_no_close_release_identifies_nothing(tmp_path, ffmpeg) -> None:
    files = [_m4a(tmp_path / "01.m4a")]
    assert release.match_folder(files, tag_album=lambda s: SimpleNamespace(candidates=[_match(0.4)])) is None
    assert release.match_folder(files, tag_album=lambda s: SimpleNamespace(candidates=[])) is None
    assert release.match_folder([], tag_album=lambda s: pytest.fail("no files, no search")) is None


def test_identify_runs_beets_in_a_subprocess(tmp_path, caplog) -> None:
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="noise\n" + json.dumps(
            {"mb_albumid": "CD94", "album": "Ready to Die", "tracks": 17, "missing": 0, "unmatched": 1}),
            stderr="")

    with caplog.at_level("INFO", logger="music_scan.release"):
        found = release.identify(tmp_path, run=run)
    assert found["mb_albumid"] == "CD94" and calls[0][1:] == ["-m", "music_scan.release", str(tmp_path)]
    assert "[RELEASE]" in caplog.text
    assert release.identify(tmp_path, run=lambda c, **kw: SimpleNamespace(returncode=0, stdout="null",
                                                                            stderr="")) is None
    with pytest.raises(RuntimeError):
        release.identify(tmp_path, run=lambda c, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"))


def test_import_rest_imports_the_unmatched_files_as_they_are(tmp_path) -> None:
    quarantine, inbox = tmp_path / "quarantine", tmp_path / "inbox"
    job = quarantine / "usenet" / "keep" / "Job"
    job.mkdir(parents=True)
    for name in ("01.flac", "02.flac"):
        (job / name).write_bytes(b"x")
    calls = []

    def run_import(folder, asis=False, config=None):
        calls.append((folder, asis, config.read_text()))
        (folder / "01.flac").unlink()  # beets imported it; 02 it skipped

    assert release.import_rest(quarantine, inbox, "keep", "Job", run_import=run_import) == 2
    assert calls == [(inbox / "usenet" / "keep" / "Job", True, "import:\n  duplicate_action: keep\n")]
    assert sorted(p.name for p in job.iterdir()) == ["02.flac"]  # skipped: back in quarantine
    assert not (inbox / "usenet" / "keep" / "Job").exists()
    assert release.import_rest(quarantine, inbox, "keep", "Nothing", run_import=run_import) == 0


def test_claim_stamps_this_imports_usenet_items() -> None:
    def usenet(id_, added=200.0, job="RTD-Job", **f):
        return FakeItem(id_, added, via="usenet", usenet_job=job, **f)

    fresh = usenet(1, sources="keep", title="Juicy")
    other_playlist = usenet(2, sources="later")
    older = usenet(3, 50.0, sources="keep")
    spotdl = FakeItem(4, 200.0, via="spotdl", sources="keep")
    held = usenet(5, sources="keep", album_record="OTHER")
    # Another job's leftovers the same scan imported (#284).
    leftover = usenet(6, sources="keep", job="Hello Sadness-Job")
    mine = release.claim([fresh, other_playlist, older, spotdl, held, leftover], "keep", 100.0, "RTD", "r1",
                         "RTD-Job")
    assert mine == [fresh] and fresh["album_record"] == "RTD" and fresh["release"] == "r1" and fresh.stored == 1
    assert not leftover.get("album_record")


RTD = {"name": "Ready to Die (The Remaster)", "artist": "The Notorious B.I.G."}


def _legacy(id_, **f):
    return FakeItem(id_, via="usenet", album="Ready to Die", albumartist="The Notorious B.I.G.", **f)


def test_copies_takes_every_other_copy_and_the_legacy_items_of_the_album() -> None:
    kept = FakeItem(1, album_record="RTD", release="r2")
    earlier = FakeItem(2, album_record="RTD", release="r1")
    legacy = _legacy(3, spotify_album_id="RTD")
    legacy_no_album = _legacy(4)
    another_album = _legacy(5, spotify_album_id="BESTOF")
    other_record = FakeItem(6, album_record="OTHER", release="r9")
    spotdl = FakeItem(7, via="spotdl", album="Ready to Die", albumartist="The Notorious B.I.G.",
                      spotify_album_id="RTD", sources="aaaaaah")  # a loose copy: supersede's
    items = [kept, earlier, legacy, legacy_no_album, another_album, other_record, spotdl]
    assert release.copies(items, "RTD", RTD, "r2", None) == [earlier, legacy, legacy_no_album]
    assert release.copies(items, "RTD", RTD, None, "r1") == [earlier]


def test_mixed_reasons_name_what_the_track_rules_did() -> None:
    record = {"status": "imported", "blocklist": ["r0"], "rejected_tracks": {"keep": ["s3"]}, "gaps": ["s3"]}
    items = [FakeItem(1, via="usenet", spotify_album_id="RTD"), FakeItem(2, via="spotdl", spotify_album_id="RTD")]
    reasons = release.mixed_reasons("RTD", record, items)
    assert len(reasons) == 4 and "1 spotdl track(s) on the album" in reasons
    whole = [FakeItem(1, via="usenet", spotify_album_id="RTD", album_record="RTD")]
    assert release.mixed_reasons("RTD", record, whole) == []
    assert release.mixed_reasons("RTD", {"status": "fallback", "fallback_from": "failed"}, items) == []


def test_replaced_copies_by_album_and_artist_tags(tmp_path, ffmpeg) -> None:
    mine = _m4a(tmp_path / "3108-05 - Warning.m4a", album="Ready to Die", albumartist="The Notorious B.I.G.",
                artist="The Notorious B.I.G.")
    _m4a(tmp_path / "77-01 - Hypnotize.m4a", album="Life After Death", albumartist="The Notorious B.I.G.",
         artist="The Notorious B.I.G.")
    assert release.replaced_copies(RTD, tmp_path) == [mine]
    assert release.replaced_copies(RTD, tmp_path / "absent") == []


def test_copies_tells_two_imports_of_one_release_apart() -> None:
    """After music-album-reset --reimport the same release may be grabbed again: the old import goes."""
    old = FakeItem(1, album_record="RTD", release="r1@100")
    new = FakeItem(2, album_record="RTD", release="r1@200")
    assert release.copies([old, new], "RTD", RTD, "r1@200", None) == [old]


def test_copies_takes_spotdls_top_ups_but_not_a_loose_copy_another_playlist_lists() -> None:
    """Mellon Collie's 14 tracks spotdl fetched (#272/#205) go with a reset's re-import (#279);
    a spotdl copy aaaaaah also lists stays, for supersede."""
    record = {**RTD, "playlists": {"keep": [], "later": []}}
    topup = FakeItem(1, via="spotdl", spotify_album_id="RTD", sources="keep")
    unlinked = FakeItem(2, via="spotdl", spotify_album_id="RTD", sources="")
    loose = FakeItem(3, via="spotdl", spotify_album_id="RTD", sources="keep,aaaaaah")
    elsewhere = FakeItem(4, via="spotdl", spotify_album_id="OTHER", sources="keep")
    assert release.copies([topup, unlinked, loose, elsewhere], "RTD", record, "r1@1", None) == [topup, unlinked]
    assert release.copies([topup], "RTD", record, None, "r1@1") == []


def test_album_query_falls_back_to_the_key_for_twins() -> None:
    """The audit prints a reset command that selects one album (#289)."""
    records = {
        "std": {"artist": "Los Campesinos!", "name": "Hold on Now, Youngster..."},
        "dlx": {"artist": "Los Campesinos!", "name": "Hold On Now, Youngster... (2018 Deluxe Remaster)"},
        "tw1": {"artist": "Braids", "name": "Euphoric Recall"},
        "tw2": {"artist": "Braids", "name": "Euphoric Recall"},
        "q": {"artist": "Vince Staples", "name": "Summertime '06"},
    }
    assert release.album_query(records, "std", "Los Campesinos! — Hold on Now, Youngster...") == \
        "Los Campesinos! — Hold on Now, Youngster..."
    assert release.album_query(records, "tw2", "Braids — Euphoric Recall") == "tw2"
    assert release.album_query(records, "q", "Vince Staples — Summertime '06") == "Vince Staples — Summertime '\\''06"


class FakeLib:
    def __init__(self, items):
        self.items = items

    def all_items(self):
        return list(self.items)


@pytest.fixture
def quarantined(monkeypatch):
    from music_scan import rollback

    gone: list = []
    monkeypatch.setattr(rollback, "quarantine", lambda items: gone.extend(items) or [])
    return gone


BOYS = {"name": "Boys and Girls in America", "artist": "The Hold Steady", "playlists": {"keep": []}}


def _cd(release_id, n, **f):
    """Track *n* of a release import: ISRC USHS0600n, as MusicBrainz tags it."""
    return FakeItem(100 + n if release_id == "cd@2" else n, via="usenet", album_record="BOYS", release=release_id,
                    isrc=f"USHS0600{n:02d}", title=f"Track {n}", **f)


def test_retire_keeps_the_recordings_a_smaller_edition_lacks_as_extras(quarantined) -> None:
    """Boys and Girls in America (#287): a reimport of the 11-track CD over the
    deluxe retires the 11 tracks the CD holds; the deluxe's bonus tracks and
    demos, and a spotdl top-up the CD doesn't have, stay on the album."""
    deluxe = [_cd("deluxe@1", n) for n in range(1, 18)]
    cd = [_cd("cd@2", n) for n in range(1, 12)]
    # A legacy copy matched by MusicBrainz recording only, and one with no identity at all.
    legacy = {"via": "usenet", "album": "Boys and Girls in America", "albumartist": "The Hold Steady"}
    cd[0]["mb_trackid"] = "rec-1"
    legacy_held = FakeItem(50, title="Track 1", mb_trackid="rec-1", **legacy)
    legacy_unknown = FakeItem(51, title="Track 3", **legacy)  # the same title: not the same recording
    topup = FakeItem(52, via="spotdl", spotify_album_id="BOYS", sources="keep", spotify_ids="SPX",
                     title="Stevie Nix (Live)")
    lib = FakeLib([*deluxe, *cd, legacy_held, legacy_unknown, topup])
    assert release.retire(lib, "BOYS", BOYS, "cd@2", None) == 12
    assert quarantined == [*deluxe[:11], legacy_held]
    extras = [*deluxe[11:], legacy_unknown, topup]
    for item in extras:
        assert item["album_record"] == "BOYS" and item[release.EXTRA] == "1" and item["canon_wait"] == "1"
        assert item.stored == 1
    assert all(not i.get(release.EXTRA) for i in cd)
    # Canon keeps them on the album, and the album is no longer mixed.
    for item in [*cd, *extras]:
        item["spotify_album_id"] = "BOYS"
    assert release.mixed_reasons("BOYS", {"status": "imported", "blocklist": ["x"]}, [*cd, *extras]) == []


def test_retire_takes_every_copy_a_same_edition_reimport_holds(quarantined) -> None:
    old = [_cd("deluxe@1", n) for n in range(1, 12)]
    new = [_cd("cd@2", n) for n in range(1, 12)]
    topup = FakeItem(52, via="spotdl", spotify_album_id="BOYS", sources="keep", isrc="USHS060005")
    assert release.retire(FakeLib([*old, *new, topup]), "BOYS", BOYS, "cd@2", None) == 12
    assert quarantined == [*old, topup]
    assert not any(i.get(release.EXTRA) for i in [*old, *new])


def test_retire_extras_go_once_a_later_release_holds_them(quarantined) -> None:
    extra = _cd("deluxe@1", 12, album_extra="1")
    again = [_cd("cd@2", n) for n in range(1, 12)] + [FakeItem(200, album_record="BOYS", release="cd@2",
                                                              isrc="USHS060012")]
    assert release.retire(FakeLib([extra, *again]), "BOYS", BOYS, "cd@2", None) == 1
    assert quarantined == [extra]


def test_retire_with_drop_takes_the_whole_release(quarantined) -> None:
    """A rolled-back release (another album) goes whole: its files are the wrong ones."""
    kept = [_cd("deluxe@1", n) for n in range(1, 3)]
    wrong = [FakeItem(9, album_record="BOYS", release="wrong@3", isrc="XX0000000001")]
    assert release.retire(FakeLib([*kept, *wrong]), "BOYS", BOYS, None, "wrong@3") == 1
    assert quarantined == wrong and not wrong[0].get(release.EXTRA)


def test_split_replaced_deletes_only_what_the_library_holds(tmp_path, ffmpeg) -> None:
    """--replaced (#287): a mix of superseded files and only copies."""
    from music_scan.identity import Recordings

    by_isrc = _m4a(tmp_path / "1-01 - Stuck Between Stations.m4a", isrc="USHS06000001")
    by_mbid = _m4a(tmp_path / "2-02 - Chips Ahoy!.m4a", mb_trackid="rec-2")
    bonus = _m4a(tmp_path / "3-12 - Girls Like Status.m4a", isrc="USHS06000012", mb_trackid="rec-12")
    bare = _m4a(tmp_path / "4-03 - Hot Soft Light.m4a", title="Hot Soft Light")  # the title alone is no identity
    library = Recordings([FakeItem(1, isrc="USHS06000001"), FakeItem(2, mb_trackid="rec-2"),
                          FakeItem(3, title="Hot Soft Light")])
    held, only = release.split_replaced([by_isrc, by_mbid, bonus, bare, tmp_path / "gone.m4a"], library)
    assert held == [by_isrc, by_mbid]
    assert only == [bonus, bare, tmp_path / "gone.m4a"]


def test_purge_replaced_deletes_superseded_files_after_the_retention_only(tmp_path, ffmpeg) -> None:
    """#288: a superseded file goes once it has sat 14 days in quarantine/replaced/; an only copy never."""
    import time

    from music_scan.identity import Recordings

    superseded = _m4a(tmp_path / "1-01 - Stuck Between Stations.m4a", isrc="USHS06000001")
    only = _m4a(tmp_path / "3-12 - Girls Like Status.m4a", isrc="USHS06000012")
    library = Recordings([FakeItem(1, isrc="USHS06000001")])
    assert release.purge_replaced(tmp_path, library, 14) == {"superseded": 1, "only_copy": 1, "purged": 0}
    assert superseded.exists()
    assert release.purge_replaced(tmp_path, library, 0, now=time.time() + 30 * 86400)["purged"] == 0
    later = time.time() + 15 * 86400
    assert release.purge_replaced(tmp_path, library, 14, now=later) == {"superseded": 0, "only_copy": 1, "purged": 1}
    assert not superseded.exists() and only.exists()
    assert release.purge_replaced(tmp_path / "absent", library, 14) == {"superseded": 0, "only_copy": 0,
                                                                         "purged": 0}


def test_a_merge_loser_is_superseded_while_its_keeper_is_in_the_library(tmp_path, ffmpeg) -> None:
    """#303: a loser with no identity tags, or only an mb_trackid its keeper lacks, is
    superseded by the keeper's merge record; once the keeper leaves, it is an only copy."""
    import time

    from music_scan.identity import Recordings

    bare = _m4a(tmp_path / "1870-04 - Love Cry (Joy Orbison remix).m4a", title="Love Cry")
    by_mbid = _m4a(tmp_path / "3377-12 - Ava Adore.m4a", mb_trackid="rec-loser")
    lookalike = _m4a(tmp_path / "99-12 - Ava Adore.m4a", title="Ava Adore")  # no record, same title
    files = [bare, by_mbid, lookalike]
    keepers = [FakeItem(3724, title="Love Cry", merged_files=bare.name),
               FakeItem(3411, title="Ava Adore", mb_trackid="rec-keeper", merged_files=by_mbid.name)]
    assert release.split_replaced(files, Recordings(keepers)) == ([bare, by_mbid], [lookalike])
    later = time.time() + 15 * 86400
    assert release.purge_replaced(tmp_path, Recordings(keepers), 14, now=later) == {
        "superseded": 0, "only_copy": 1, "purged": 2}
    assert not bare.exists() and not by_mbid.exists() and lookalike.exists()

    gone = _m4a(tmp_path / "329-10 - Killer Parties.m4a")
    assert release.split_replaced([gone], Recordings(keepers)) == ([], [gone])  # its keeper left the library


def test_retention_days_setting(monkeypatch) -> None:
    assert release.retention_days() == 14
    monkeypatch.setenv("REPLACED_RETENTION_DAYS", "0")
    assert release.retention_days() == 0
    monkeypatch.setenv("REPLACED_RETENTION_DAYS", "soon")
    assert release.retention_days() == 14


class _Track(FakeItem):
    """A FakeItem with the attributes tag_album_ids reads."""

    def __init__(self, id_, title, track, added=100.0, **fields):
        super().__init__(id_, added, title=title, **fields)
        self.title, self.artist, self.albumartist = title, "Bob Dylan", "Bob Dylan"
        self.disc, self.track, self.tracktotal = 1, track, 10


class _SourceLib(FakeLib):
    def items_by_source(self, source):
        return [i for i in self.items if source in (i.get("sources") or "").split(",")]


BOTT = {"name": "Blood On The Tracks", "artist": "Bob Dylan", "playlists": {"keep": []}}
BOTT_TITLES = ["Tangled Up in Blue", "Simple Twist of Fate", "You're a Big Girl Now", "Idiot Wind",
               "You're Gonna Make Me Lonesome When You Go", "Meet Me in the Morning", "Lily, Rosemary and the Jack of Hearts",
               "If You See Her, Say Hello", "Shelter from the Storm", "Buckets of Rain"]


def test_retire_held_retires_the_extras_tag_album_ids_shows_the_release_holds(quarantined) -> None:
    """Blood On The Tracks (#294): beets matched the release to nothing in
    MusicBrainz, so at retire it held no recording and every legacy copy became
    an extra.  Once tag_album_ids gives it Spotify IDs by disc/track, the
    extras it holds go; the one whose recording it still lacks stays."""
    from music_scan.scan import tag_album_ids

    legacy = {"via": "usenet", "album": "Blood On The Tracks", "albumartist": "Bob Dylan", "sources": "keep"}
    held = [_Track(2931, "Tangled Up in Blue", 1, added=10.0, spotify_ids="sp1", isrc="USSM10000001", **legacy),
            _Track(2934, "Idiot Wind", 4, added=10.0, spotify_ids="sp4", **legacy)]
    # Same title as the release's track 9, another recording: titles never count.
    lacking = _Track(2938, "Shelter from the Storm", 9, added=10.0, spotify_ids="sp-live", **legacy)
    new = [_Track(n, t, n, added=200.0, album_record="BOTT", release="r@200", via="usenet", sources="keep")
           for n, t in enumerate(BOTT_TITLES, 1)]
    lib = _SourceLib([*held, lacking, *new])

    assert release.retire(lib, "BOTT", BOTT, "r@200", None) == 0
    assert all(i.get(release.EXTRA) for i in [*held, lacking])
    assert release.retire_held(lib) == 0  # nothing to go on yet

    tracks = [[t, "Bob Dylan", f"sp{n}", "USSM10000001" if n == 1 else None, 1, n]
              for n, t in enumerate(BOTT_TITLES, 1)]
    assert tag_album_ids(lib, "keep", tracks, 150.0, 10) == 10
    assert new[0]["spotify_ids"] == "sp1" and new[3]["spotify_ids"] == "sp4"
    assert release.retire_held(lib) == 2
    assert quarantined == held
    assert lacking.get("spotify_ids") == "sp-live" and new[8]["spotify_ids"] == "sp9"


def test_retire_held_leaves_extras_the_release_lacks_and_other_records_alone(quarantined) -> None:
    bonus = _cd("deluxe@1", 12, album_extra="1")
    cd = [_cd("cd@2", n) for n in range(1, 12)]
    # Another record's release holds the bonus track's recording: not this album's.
    other = FakeItem(300, album_record="OTHER", release="x@1", isrc="USHS060012")
    # An extra of a record whose release is gone (rolled back): nothing holds it.
    orphan = FakeItem(301, album_record="GONE", album_extra="1", isrc="USHS060012")
    assert release.retire_held(FakeLib([bonus, *cd, other, orphan])) == 0
    assert quarantined == []


class _Audio(FakeItem):
    """A FakeItem with what music_scan.dupes reads and a merge writes."""

    def __init__(self, id_, title, length=200.0, **fields):
        super().__init__(id_, title=title, artist="The Hold Steady", **fields)
        self.title, self.artist, self.albumartist = title, "The Hold Steady", "The Hold Steady"
        self.length, self.bitrate, self.path = length, 256000, f"/nonexistent/{id_}.m4a".encode()
        self.removed = self.written = False

    def try_write(self):
        self.written = True

    def remove(self, delete=False):
        assert delete is False
        self.removed = True


def _stay_positive(extra_title="Lord, I'm Discouraged", extra_length=230.0, extra_acoustid="aid-6",
                   **extra_fields):
    """Stay Positive (#301): the reimport's track 6 and a legacy extra of it
    with none of its IDs (beets matched the release to another MB recording)."""
    track = _Audio(3786, "Lord, I'm Discouraged", length=231.0, album_record="SP", release="r@2", via="usenet",
                   sources="keep", spotify_ids="sp6", isrc="USVR10800006", mb_trackid="rec-new",
                   acoustid_id="aid-6")
    extra = _Audio(2721, extra_title, length=extra_length, album_record="SP", album_extra="1", via="usenet",
                   sources="keep,road", isrc="USVR10899999", acoustid_id=extra_acoustid, **extra_fields)
    return track, extra


def test_retire_held_merges_an_extra_the_release_holds_the_audio_of(quarantined, caplog) -> None:
    track, extra = _stay_positive(spotify_ids="sp-old")
    other = _Audio(3790, "Navy Sheets", album_record="SP", release="r@2", acoustid_id="aid-8")
    caplog.set_level("INFO")
    assert release.retire_held(FakeLib([track, other, extra]), decoder=lambda fp: []) == 1
    assert quarantined == []  # merged, not quarantined
    assert extra.removed and not track.removed and not other.removed
    assert track["spotify_ids"].split(",") == ["sp6", "sp-old"]
    assert track["sources"].split(",") == ["keep", "road"]
    assert "USVR10899999" in track["isrc"] and track["mb_trackid"] == "rec-new"
    assert track["canon_wait"] == "1"  # the next canon moves it off its ".1" path
    assert "2721" in caplog.text and "(extra, same audio): merged into 3786" in caplog.text


def test_retire_held_leaves_an_extra_whose_audio_the_release_lacks(quarantined) -> None:
    """A deluxe bonus track under the standard edition: another fingerprint, another title."""
    track, extra = _stay_positive(extra_title="Two Handed Handshake", extra_acoustid="aid-bonus")
    assert release.retire_held(FakeLib([track, extra]), decoder=lambda fp: []) == 0
    assert not extra.removed and quarantined == [] and not track.get("canon_wait")


@pytest.mark.parametrize("fields", [
    {"extra_length": 260.0},  # AcoustID match, lengths 30 s apart
    {"extra_title": "Constructive Summer"},  # AcoustID match, another title: wrong audio
    {"extra_acoustid": None},  # title and artist only
], ids=["lengths-apart", "titles-differ", "title-artist"])
def test_retire_held_never_merges_an_uncertain_pair(quarantined, fields) -> None:
    track, extra = _stay_positive(**fields)
    assert release.retire_held(FakeLib([track, extra]), decoder=lambda fp: []) == 0
    assert not extra.removed and track["spotify_ids"] == "sp6" and track["sources"] == "keep"


def test_retire_held_leaves_an_extra_two_release_items_hold_the_audio_of(quarantined) -> None:
    """Two release items one recording by ISRC (40 Bucks): no keeper to choose, left to the operator."""
    track, extra = _stay_positive()
    twin = _Audio(3806, "Lord, I'm Discouraged", length=231.0, album_record="SP", release="r@2",
                  isrc="USVR10800006")
    assert release.retire_held(FakeLib([track, twin, extra]), decoder=lambda fp: []) == 0
    assert not extra.removed

def test_mixed_reasons_a_whole_import_with_a_loose_spotdl_copy_is_not_mixed() -> None:
    """Zero 7 Simple Things (#295): imported whole since #279 after a reset by
    hand, plus one spotdl copy another playlist also lists, which retire leaves
    on purpose.  A reimport would grab the same release again."""
    record = {"name": "Simple Things", "artist": "Zero 7", "status": "imported", "blocklist": ["r0"],
              "playlists": {"keep": []}}
    release_items = [FakeItem(n, via="usenet", spotify_album_id="Z7", album_record="Z7", release="v@1")
                     for n in range(1, 12)]
    loose = FakeItem(20, via="spotdl", spotify_album_id="Z7", sources="keep,aaaaaah")
    extra = FakeItem(21, via="usenet", spotify_album_id="Z7", album_record="Z7", album_extra="1")
    assert release.mixed_reasons("Z7", record, [*release_items, loose, extra]) == []
    # Nor any other leftover: the import already retired every copy a reimport would.
    stray = FakeItem(22, via="usenet", spotify_album_id="Z7", album="Simple Things (Remastered)")
    assert release.mixed_reasons("Z7", record, [*release_items, loose, stray]) == []


def test_mixed_reasons_before_279_ignores_loose_copies_but_not_top_ups() -> None:
    """A pre-#279 album still counts spotdl's top-ups (a reimport retires them),
    not a copy another playlist lists, and keeps its history's reasons."""
    record = {"status": "imported", "playlists": {"keep": []}}
    legacy = FakeItem(1, via="usenet", spotify_album_id="RTD")
    loose = FakeItem(2, via="spotdl", spotify_album_id="RTD", sources="keep,aaaaaah")
    topup = FakeItem(3, via="spotdl", spotify_album_id="RTD", sources="keep")
    assert release.mixed_reasons("RTD", record, [legacy, loose]) == []
    assert release.mixed_reasons("RTD", record, [legacy, loose, topup]) == ["1 spotdl track(s) on the album"]
    assert release.mixed_reasons("RTD", {**record, "gaps": ["s3"], "blocklist": ["r0"]}, [legacy, loose]) == [
        "imported short of Spotify's tracklist ([PART]/[GAP])",
        "1 release(s) blocklisted before (their unmatched tracks rolled back)",
    ]


ALPHA = {"name": "Alpha", "artist": "Test Artist"}
ALPHA_PLUS = {"name": "Alpha Plus", "artist": "Test Artist"}


def test_group_replaced_by_claiming_record_and_unclaimed(tmp_path, ffmpeg) -> None:
    """#298: a file under every record that claims it, the rest unclaimed."""
    alpha = _m4a(tmp_path / "11-01 - One.m4a", album="Alpha", albumartist="Test Artist")
    both = _m4a(tmp_path / "12-02 - Two.m4a", album="Alpha Plus", albumartist="Test Artist")
    other = _m4a(tmp_path / "13-01 - Three.m4a", album="Gamma", albumartist="Test Artist")
    claimed, unclaimed = release.group_replaced({"A": ALPHA, "AP": ALPHA_PLUS},
                                                [alpha, both, other, tmp_path / "gone.m4a"])
    assert claimed == {"A": [alpha, both], "AP": [both]}
    assert unclaimed == [other, tmp_path / "gone.m4a"]


def test_list_replaced_counts_what_the_scan_pushes(tmp_path, ffmpeg, caplog) -> None:
    """#298: every only copy, by album with its --replaced command, then the
    unclaimed ones with their item ID and tags; the totals are the metric's."""
    import logging

    from music_scan.identity import Recordings

    _m4a(tmp_path / "21-01 - One.m4a", album="Alpha", albumartist="Test Artist", isrc="XX0000000001")
    only = _m4a(tmp_path / "22-02 - Two.m4a", album="Alpha", albumartist="Test Artist", isrc="XX0000000002")
    loser = _m4a(tmp_path / "23-07 - Seven.m4a", album="Gamma", albumartist="Test Artist", title="Seven",
                 isrc="XX0000000007")
    bare = _m4a(tmp_path / "loose.m4a")
    library = Recordings([FakeItem(1, isrc="XX0000000001")])
    records = {"A": ALPHA, "AP": ALPHA_PLUS}
    with caplog.at_level(logging.INFO, logger="music_scan.release"):
        counts = release.list_replaced(records, tmp_path, library)
    assert counts == {k: n for k, n in release.purge_replaced(tmp_path, library, 0).items() if k != "purged"}
    assert counts == {"superseded": 1, "only_copy": 3}
    out = caplog.text
    assert "[ALBUM] Test Artist — Alpha (A): 1 only copies" in out
    assert "music-album-audit-releases --replaced 'Test Artist — Alpha'" in out
    assert f"[ONLYCOPY] {only}\n" in out
    assert "Alpha Plus" not in out  # claims none of them
    assert f"[ONLYCOPY] {loser}: item 23; Test Artist — Seven [Gamma]; isrc XX0000000007" in out
    assert f"[ONLYCOPY] {bare}: no item ID; ? — ? [?]; no identity tags" in out
    assert "3 only copies in" in out and "1 on 1 album(s), 2 unclaimed; 1 superseded" in out


def test_audit_replaced_without_a_query_only_lists(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["music-album-audit-releases", "--replaced", "--apply", "--only-copies"])
    with pytest.raises(SystemExit) as exc:
        release.audit_main()
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Extras sharing a title with the release (#306)
# ---------------------------------------------------------------------------

def _stop_making_sense():
    """Stop Making Sense (#306): the 1984 LP's Psycho Killer, two legacy extras of it
    from the 1999 and 2023 editions (other edits, no shared ID, no AcoustID), and a
    legacy extra the LP lacks altogether."""
    track = _Audio(3730, "Psycho Killer", length=258.0, album_record="SMS", release="lp@1", spotify_ids="sp-pk",
                   isrc="USWB18400001")
    se = _Audio(2555, "Psycho Killer - Live", length=268.0, album_record="SMS", album_extra="1", isrc="USWB19900001")
    dx = _Audio(2588, "Psycho Killer (Live) [2023 Remaster]", length=288.0, album_record="SMS", album_extra="1",
                spotify_ids="sp-pk-dx")
    bonus = _Audio(2590, "Genius of Love", length=300.0, album_record="SMS", album_extra="1")
    return track, se, dx, bonus


def test_retire_held_flags_extras_sharing_a_title_with_the_release_once(quarantined, caplog) -> None:
    track, se, dx, bonus = _stop_making_sense()
    lib = FakeLib([track, se, dx, bonus])
    with caplog.at_level("INFO"):
        assert release.retire_held(lib, decoder=lambda fp: []) == 0
    assert not se.removed and not dx.removed and quarantined == []  # reported, never merged
    assert se[release.TWIN] == dx[release.TWIN] == "3730" and release.TWIN not in bonus
    assert caplog.text.count("[SAMETITLE]") == 2
    assert "[SAMETITLE] 2555 The Hold Steady — Psycho Killer - Live" in caplog.text
    assert "the release's 3730 (258s) has its title, but no ID agrees" in caplog.text
    caplog.clear()
    stored = se.stored
    with caplog.at_level("INFO"):
        release.retire_held(lib, decoder=lambda fp: [])
    assert "[SAMETITLE]" not in caplog.text and se.stored == stored  # once, not every scan
    # The pair's release item went (the operator merged it the other way): the stamp goes too.
    release.retire_held(FakeLib([se, dx, bonus]), decoder=lambda fp: [])
    assert release.TWIN not in se and release.TWIN not in dx


def test_list_twins_gives_each_pair_its_merge_command(caplog) -> None:
    track, se, dx, bonus = _stop_making_sense()
    twins = release.title_twins([track, se, dx, bonus])
    assert {k: [(e.id, t.id) for e, t in v] for k, v in twins.items()} == {"SMS": [(2555, 3730), (2588, 3730)]}
    records = {"SMS": {"artist": "Talking Heads", "name": "Stop Making Sense"}}
    with caplog.at_level("INFO"):
        assert release.list_twins(records, twins, {"sp-pk": 268.0}) == 1
    assert "[SAMETITLE] Talking Heads — Stop Making Sense (SMS): 2 local-only extra(s) share a title" in caplog.text
    # The keeper is the Spotify-length copy: the extra here.
    assert "2555 Psycho Killer - Live (268s, legacy) ~ 3730 (258s): music-audit-dupes --merge 2555 3730" in caplog.text
    assert "1 album(s) with local-only extras sharing a title" in caplog.text

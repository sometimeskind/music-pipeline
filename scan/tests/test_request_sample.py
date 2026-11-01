"""music-requests fallback-sample: the MusicBrainz-built fallback's hit rate (homelab#2478)."""

from pathlib import Path

import pytest

from music_scan import request_sample


class FakeItem(dict):
    def __init__(self, id_=1, **fields):
        super().__init__(**fields)
        self.id = id_

    def get(self, key, default=None):
        return super().get(key, default)


def _rec(i: int) -> str:
    return f"00000001-0000-4000-8000-{i:012x}"


def test_candidates_are_held_albums_with_a_release_group_and_one_release():
    records = {
        "1": {"status": "imported", "release_group": "RG1", "artist": "Band", "name": "One"},
        "2": {"status": "imported", "release_group": None},  # no release group
        "3": {"status": "wanted", "release_group": "RG3"},  # not held
        "4": {"status": "have", "release_group": "RG4"},  # two releases
    }
    items = [FakeItem(1, album_record="1", mb_albumid="R1", mb_trackid=_rec(1)),
             FakeItem(2, album_record="1", mb_albumid="R1", mb_trackid=_rec(2)),
             FakeItem(3, album_record="1", mb_albumid="RX", album_extra="1"),  # an extra doesn't count
             FakeItem(4, album_record="2", mb_albumid="R2"), FakeItem(5, album_record="3", mb_albumid="R3"),
             FakeItem(6, album_record="4", mb_albumid="R4"), FakeItem(7, album_record="4", mb_albumid="R5")]
    [album] = request_sample.candidates(records, items)
    assert (album.key, album.release, album.label) == ("1", "R1", "Band — One")
    assert set(album.held) == {_rec(1), _rec(2)}


def test_sample_judges_each_track_and_keeps_nothing(tmp_path: Path, clip, monkeypatch):
    monkeypatch.setattr("music_scan.guard.ACOUSTID_DELAY", 0)
    held = {_rec(i): FakeItem(i) for i in (1, 2, 3, 4, 5)}
    album = request_sample.Album("1", "Band — One", {"status": "imported"}, "R1", held)
    songs = [{"song_id": _rec(i), "url": f"https://musicbrainz.org/recording/{_rec(i)}", "duration": 30}
             for i in (1, 2, 3, 4, 5, 6)]
    asked = []

    def songs_of(record):
        asked.append(record["tracklist_release"])
        return songs

    def download(batch, folder):
        clip(folder / "a.m4a", ("tone", 30), url=batch[0]["url"])  # accepted, the library's audio
        clip(folder / "b.m4a", ("tone", 30), url=batch[1]["url"])  # AcoustID names another recording
        clip(folder / "c.m4a", ("tone", 20), url=batch[2]["url"])  # too short
        clip(folder / "d.m4a", ("tone", 30), url=batch[3]["url"])  # AcoustID knows no recording for it
        clip(folder / "e.m4a", ("tone", 30), url=batch[4]["url"])  # the same, and the library's audio
        # the sixth: spotdl found nothing

    found = {_rec(1): {_rec(1): 0.95}, _rec(2): {_rec(9): 0.95}, _rec(4): {}, _rec(5): {}}

    def recordings(path):
        from music_scan.guard import song_url

        return found[song_url(path).rsplit("/", 1)[1]]

    [(_, outcomes)] = request_sample.sample([album], songs_of, download, recordings,
                                            lambda path, item: item.id in (1, 2, 5), tmp_path / "scratch")
    assert asked == ["R1"]
    assert [(o.verdict, o.same_audio) for o in outcomes] == [
        ("accepted", True), ("fingerprint", True), ("duration", False), ("acoustid-unknown", False),
        ("acoustid-unknown", True), ("not found", None)]
    assert not (tmp_path / "scratch" / "1").exists()
    line = request_sample.summary(outcomes)
    assert line == ("6 track(s); 1 accepted; 1 of them the library's audio, 0 not; 4 rejected "
                    "(duration 1, fingerprint 1, acoustid-unknown 2), 2 of them the library's audio; "
                    "2 unknown to AcoustID, 1 of them the library's audio; 1 not found by spotdl")


def test_music_requests_fallback_sample_refuses_a_sample_too_large():
    from music_fetch import album_requests as req
    from music_fetch import album_requests_cli as cli

    with pytest.raises(req.Refused, match="--n must be 1 to 25"):
        cli.run(cli._parser().parse_args(["fallback-sample", "--n", "26"]))

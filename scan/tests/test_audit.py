"""Tests for music_scan.audit — music-audit-lengths (#165)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_scan import audit
from music_scan.audit import audit as run_audit
from music_scan.audit import find_song, off_by


URL = "https://open.spotify.com/track/"
DEMO = "https://www.youtube.com/watch?v=demo0000000"
STUDIO = "https://www.youtube.com/watch?v=studio00000"


def _item(id_, title, sources, length, **data):
    item = MagicMock(id=id_, title=title, artist="Artist", albumartist="Artist", length=length,
                     path=f"/lib/{title}.m4a".encode())
    data["sources"] = sources
    item.get = lambda k, d=None: data.get(k, d)
    return item


def _song(sid, name="Song", duration=200, isrc=None):
    return {"name": name, "artists": ["Artist"], "url": URL + sid, "song_id": sid, "isrc": isrc, "duration": duration}


def test_off_by_needs_both_fraction_and_seconds() -> None:
    assert off_by(200, 230, 0.10, 5)
    assert not off_by(200, 215, 0.10, 5)  # 7.5%
    assert not off_by(30, 34, 0.10, 5)  # 13% but only 4s


def test_audit_flags_wrong_length_by_spotify_id() -> None:
    right = _item(1, "Right", "wedding", 201.0, spotify_ids="R")
    wrong = _item(2, "Calico Skies", "wedding", 170.0, spotify_ids="C", comments=DEMO)
    suspects, counts = run_audit([right, wrong], {"wedding": [_song("R"), _song("C", "Calico Skies", 150)]})
    assert [(s.item, s.expected, s.delta) for s in suspects] == [(wrong, 150, 20.0)]
    assert counts == {"matched": 2, "unmatched": 0, "no_duration": 0}


def test_audit_finds_items_outside_the_playlist_by_isrc_but_words_only_within_it() -> None:
    by_isrc = _item(1, "Song", "keep", 300.0, isrc="GB1")
    by_words = _item(2, "Other", "keep", 300.0)
    suspects, counts = run_audit(
        [by_isrc, by_words],
        {"mood": [_song("X", "Song", isrc="GB1"), _song("Y", "Other")]},
    )
    assert [s.item for s in suspects] == [by_isrc]
    assert counts["unmatched"] == 1


def test_audit_lists_every_playlist_of_a_suspect_once() -> None:
    wrong = _item(1, "Song", "wedding,mood", 100.0, spotify_ids="S")
    suspects, _ = run_audit([wrong], {"wedding": [_song("S")], "mood": [_song("S")]})
    assert len(suspects) == 1 and suspects[0].playlists == ["wedding", "mood"]


def test_audit_counts_entries_without_duration() -> None:
    item = _item(1, "Song", "a", 100.0, spotify_ids="S")
    suspects, counts = run_audit([item], {"a": [_song("S", duration=None)]})
    assert suspects == [] and counts["no_duration"] == 1


def test_find_song_by_any_spotify_id() -> None:
    item = _item(1, "Song", "a", 1.0, spotify_ids="ALBUM,SINGLE")
    song = _song("SINGLE")
    assert find_song(item, {"a": [_song("OTHER"), song]}) is song


def test_run_reads_playlists_from_item_pages_only(monkeypatch) -> None:
    """Every configured playlist (nosync too) is read through SpotifyPlaylists.songs."""
    from music_fetch.config import PlaylistConfig as Playlist

    pls = [Playlist(name="aaaaaaah", url="u1"), Playlist(name="wedding", url="u2", nosync=True)]
    reader = MagicMock()
    reader.songs.return_value = []
    lib = MagicMock()
    lib.__enter__.return_value.all_items.return_value = []
    with patch("music_fetch.config.load_playlists", return_value=pls), \
         patch("music_fetch.spotdl_ops.SpotifyPlaylists", return_value=reader), \
         patch("music_scan.library.MusicLibrary", return_value=lib):
        audit.run()
    assert [c.args[0] for c in reader.songs.call_args_list] == ["u1", "u2"]


# ---------------------------------------------------------------------------
# --replace: the known case, Calico Skies in wedding was spotdl's pick of a demo
# ---------------------------------------------------------------------------

@pytest.fixture
def calico(tmp_path: Path, clip):
    """A library item whose file is a 'demo' (60s) of a 40s Spotify track."""
    from beets.library import Item

    from music_scan.library import MusicLibrary

    libdir = tmp_path / "library"
    path = libdir / "Paul McCartney" / "Flaming Pie" / "06 - Calico Skies.m4a"
    path.parent.mkdir(parents=True)
    clip(path, ("tone", 60))
    lib = MusicLibrary(tmp_path / "library.db", libdir)
    item = Item(path=str(path).encode(), title="Calico Skies", artist="Paul McCartney", album="Flaming Pie",
                track=6, comments=DEMO)
    item.read()
    item.title, item.artist, item.album, item.comments = "Calico Skies", "Paul McCartney", "Flaming Pie", DEMO
    item["sources"] = "wedding,mood"
    item["spotify_ids"] = "CALICO"
    item["isrc"] = "GBCAL9700006"
    item["via"] = "spotdl"
    lib._lib.add(item)
    yield lib, item, path
    lib._lib._close()


def _fake_download(clip, seconds: float):
    def download(song, out, cookie):
        assert song["download_url"] == STUDIO, "the download must be pinned to --youtube"
        f = clip(out / "Paul McCartney - Calico Skies.m4a", ("tone", seconds))
        from mutagen.mp4 import MP4

        audio = MP4(f)
        audio["\xa9cmt"] = [song["download_url"]]
        audio.save()
        return f
    return download


def test_replace_swaps_audio_in_place_and_keeps_identity(calico, clip, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    lib, item, path = calico
    song = _song("CALICO", "Calico Skies", 40, isrc="GBCAL9700006")
    replaced = tmp_path / "quarantine" / "replaced"

    with patch("music_fetch.spotdl_ops.download_song", side_effect=_fake_download(clip, 40)):
        assert audit.replace(item, song, STUDIO, Path("cookies.txt"), replaced_dir=replaced)

    fresh = lib.get_item(item.id)
    assert audit.item_path(fresh) == path
    assert abs(fresh.length - 40) < 1
    assert fresh.comments == STUDIO
    assert (fresh["sources"], fresh["spotify_ids"], fresh["isrc"]) == ("wedding,mood", "CALICO", "GBCAL9700006")
    assert len(lib.all_items()) == 1
    backup = replaced / f"{item.id}-06 - Calico Skies.m4a"
    assert backup.exists()
    from mutagen.mp4 import MP4

    assert MP4(path)["\xa9nam"] == ["Calico Skies"]  # beets' tags were written to the new file


def test_replace_refingerprints_the_new_audio(calico, clip, tmp_path, monkeypatch) -> None:
    """The old audio's fingerprint and AcoustID ID must not stay on the item, or reach the new file (#210)."""
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    lib, item, path = calico
    item["acoustid_fingerprint"], item["acoustid_id"] = "OLDFP", "old-acid"
    item.store()
    monkeypatch.setattr(audit, "fingerprint", lambda p: "NEWFP")
    song = _song("CALICO", "Calico Skies", 40, isrc="GBCAL9700006")

    with patch("music_fetch.spotdl_ops.download_song", side_effect=_fake_download(clip, 40)):
        assert audit.replace(item, song, STUDIO, Path("cookies.txt"), replaced_dir=tmp_path / "replaced")

    fresh = lib.get_item(item.id)
    assert fresh.get("acoustid_fingerprint") == "NEWFP" and not fresh.get("acoustid_id")
    from mediafile import MediaFile

    assert MediaFile(str(path)).acoustid_fingerprint == "NEWFP"


def test_replace_clears_the_fingerprint_when_fingerprinting_fails(calico, clip, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    lib, item, _ = calico
    item["acoustid_fingerprint"] = "OLDFP"
    item.store()

    def broken(p):
        raise OSError("fpcalc failed")

    monkeypatch.setattr(audit, "fingerprint", broken)
    with patch("music_fetch.spotdl_ops.download_song", side_effect=_fake_download(clip, 40)):
        assert audit.replace(item, _song("CALICO", "Calico Skies", 40), STUDIO, Path("cookies.txt"),
                             replaced_dir=tmp_path / "replaced")
    assert not lib.get_item(item.id).get("acoustid_fingerprint")


def test_replace_refuses_a_download_that_fails_the_guard(calico, clip, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    lib, item, path = calico
    song = _song("CALICO", "Calico Skies", 40)

    with patch("music_fetch.spotdl_ops.download_song", side_effect=_fake_download(clip, 60)):
        assert not audit.replace(item, song, STUDIO, Path("cookies.txt"), replaced_dir=tmp_path / "r")
    assert abs(lib.get_item(item.id).length - 60) < 1
    assert path.exists()

    with patch("music_fetch.spotdl_ops.download_song", side_effect=_fake_download(clip, 60)):
        assert audit.replace(item, song, STUDIO, Path("cookies.txt"), force=True, replaced_dir=tmp_path / "r")


def test_replace_without_pin_refuses_the_same_video(calico, clip, tmp_path, monkeypatch) -> None:
    lib, item, path = calico

    def same_video(song, out, cookie):
        assert "download_url" not in song or song["download_url"] is None
        f = clip(out / "x.m4a", ("tone", 40))
        from mutagen.mp4 import MP4

        audio = MP4(f)
        audio["\xa9cmt"] = [DEMO]
        audio.save()
        return f

    with patch("music_fetch.spotdl_ops.download_song", side_effect=same_video):
        assert not audit.replace(item, _song("CALICO", duration=40), None, Path("c"), replaced_dir=tmp_path / "r")
    assert path.exists()

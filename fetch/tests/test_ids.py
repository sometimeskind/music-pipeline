"""Tests for music_fetch.ids: canonical YouTube and Spotify IDs (homelab#2411)."""

import pytest

from music_fetch import ids

VIDEO = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"
PLAYLIST = "37i9dQZF1DXcBWIGoYBM5M"
ALBUM = "4aawyAB9vmqN3uQ7FjRGTy"


@pytest.mark.parametrize("ref", [
    VIDEO,
    f"  {VIDEO}\n",
    WATCH,
    f"https://youtube.com/watch?v={VIDEO}",
    f"http://www.youtube.com/watch?v={VIDEO}",
    f"https://music.youtube.com/watch?v={VIDEO}&si=abc",
    f"https://youtu.be/{VIDEO}",
    f"https://youtu.be/{VIDEO}?si=Xy12&t=42",
    f"https://www.youtube.com/watch?v={VIDEO}&t=1m30s",
    f"https://www.youtube.com/watch?v={VIDEO}&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=3",
    f"https://music.youtube.com/watch?v={VIDEO}&list=OLAK5uy_kXgUzMxqiNq1aW8Ks6p5mLkKeyZxYzAbC",
    f"https://www.youtube.com/shorts/{VIDEO}",
    f"https://www.youtube.com/embed/{VIDEO}",
])
def test_youtube_accepts_a_video(ref: str) -> None:
    assert ids.youtube_id(ref) == VIDEO
    assert ids.youtube_url(ref) == WATCH


@pytest.mark.parametrize("ref", [
    "",
    "dQw4w9WgXc",  # 10 characters
    "dQw4w9WgXcQQ",  # 12
    "dQw4w9WgX!Q",
    "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
    "https://music.youtube.com/watch?list=OLAK5uy_kXgUzMxqiNq1aW8Ks6p5mLkKeyZxYzAbC",
    "https://music.youtube.com/playlist?list=OLAK5uy_kXgUzMxqiNq1aW8Ks6p5mLkKeyZxYzAbC",
    "OLAK5uy_kXgUzMxqiNq1aW8Ks6p5mLkKeyZxYzAbC",
    "https://music.youtube.com/browse/MPREb_abcdefghijk",
    "https://www.youtube.com/watch?v=dQw4w9WgXc",
    f"https://www.youtube.com/watch/{VIDEO}",
    f"https://youtu.be/{VIDEO}/extra",
    f"https://vimeo.com/watch?v={VIDEO}",
    f"https://www.youtube.com.evil.example/watch?v={VIDEO}",
    f"https://evil.example/?u=https://youtu.be/{VIDEO}",
    f"https://m.youtube.com/watch?v={VIDEO}",
    f"ftp://youtu.be/{VIDEO}",
    f"youtu.be/{VIDEO}",
    f"https://soundcloud.com/artist/{VIDEO}",
])
def test_youtube_rejects_anything_else(ref: str) -> None:
    with pytest.raises(ValueError):
        ids.youtube_id(ref)


@pytest.mark.parametrize("ref,kind", [
    (PLAYLIST, None),
    (f"https://open.spotify.com/playlist/{PLAYLIST}", "playlist"),
    (f"https://open.spotify.com/playlist/{PLAYLIST}?si=0123456789abcdef", "playlist"),
    (f"https://open.spotify.com/intl-de/playlist/{PLAYLIST}", "playlist"),
    (f"https://open.spotify.com/intl-pt-br/playlist/{PLAYLIST}?si=x&pt=y", "playlist"),
    (f"https://open.spotify.com/playlist/{PLAYLIST}/", "playlist"),
])
def test_spotify_playlist(ref: str, kind: str | None) -> None:
    assert ids.spotify_ref(ref, ("playlist", "album")) == (kind, PLAYLIST)
    assert ids.spotify_id(ref, "playlist") == PLAYLIST


@pytest.mark.parametrize("ref", [
    f"https://open.spotify.com/album/{ALBUM}",
    f"https://open.spotify.com/intl-de/album/{ALBUM}?si=abc",
])
def test_spotify_album(ref: str) -> None:
    assert ids.spotify_ref(ref, ("playlist", "album")) == ("album", ALBUM)
    assert ids.spotify_id(ref, "album") == ALBUM


@pytest.mark.parametrize("ref", [
    "abc",
    PLAYLIST + "x",
    "37i9dQZF1DXcBWIGoYBM5_",
    f"https://open.spotify.com/track/{PLAYLIST}",
    f"https://open.spotify.com/artist/{PLAYLIST}",
    f"https://open.spotify.com/playlist/{PLAYLIST}/tracks",
    f"https://evil.example/playlist/{PLAYLIST}",
    f"https://open.spotify.com.evil.example/playlist/{PLAYLIST}",
    f"spotify:playlist:{PLAYLIST}",
])
def test_spotify_rejects_anything_else(ref: str) -> None:
    with pytest.raises(ValueError):
        ids.spotify_ref(ref, ("playlist", "album"))


def test_spotify_id_rejects_the_other_kind() -> None:
    with pytest.raises(ValueError):
        ids.spotify_id(f"https://open.spotify.com/album/{ALBUM}", "playlist")


def test_spotify_url() -> None:
    assert ids.spotify_url("album", ALBUM) == f"https://open.spotify.com/album/{ALBUM}"


@pytest.mark.parametrize("ref,key", [
    (f"https://open.spotify.com/intl-de/album/{ALBUM}?si=abc", ALBUM),
    (ALBUM, ALBUM),
    ("radiohead — ok computer", "radiohead — ok computer"),
    ("artist/album", "artist/album"),
])
def test_spotify_key_reduces_urls_only(ref: str, key: str) -> None:
    assert ids.spotify_key(ref, "album") == key


def test_spotify_key_rejects_another_url() -> None:
    with pytest.raises(ValueError):
        ids.spotify_key(f"https://open.spotify.com/playlist/{PLAYLIST}", "album")

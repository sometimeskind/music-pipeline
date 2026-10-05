"""Tests for music_fetch.usenet — release matching, ranking and the grab redirect."""

from unittest.mock import MagicMock, patch

import pytest

from music_fetch.usenet import (
    Prowlarr, Release, Sabnzbd, clean_album, leftover, matches, normalise, rank, readable, tier,
)

MB = 1_000_000


def rel(title: str, size: int = 400 * MB, guid: str | None = None, cats=(3000,), grabs: int = 0) -> Release:
    return Release(
        guid=guid or title,
        title=title,
        size=size,
        indexer_id=1,
        download_url=f"http://prowlarr/1/download?file={title}",
        categories=list(cats),
        grabs=grabs,
    )


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Abbey Road (Remastered 2009)", "Abbey Road"),
        ("Rumours - Super Deluxe", "Rumours"),
        ("OK Computer OKNOTOK 1997 2017", "OK Computer OKNOTOK 1997 2017"),
        ("(What's the Story) Morning Glory?", "Morning Glory?"),
    ],
)
def test_clean_album(name: str, expected: str) -> None:
    assert clean_album(name) == expected


@pytest.mark.parametrize(
    "release, expected",
    [
        (rel("Artist-Album-WEB-FLAC-2020-GRP"), 0),
        (rel("Artist - Album (2020)", cats=(3040,)), 0),
        (rel("Artist-Album-WEB-320-2020-GRP"), 1),
        (rel("Artist-Album-WEB-V0-2020-GRP"), 2),
    ],
)
def test_tier(release: Release, expected: int) -> None:
    assert tier(release) == expected


def test_matches_needs_album_and_artist_words() -> None:
    assert matches(rel("The_Artist-Great_Album-WEB-FLAC-2020"), "The Artist", "Great Album", 10)
    assert not matches(rel("Other_Band-Great_Album-WEB-FLAC-2020"), "The Artist", "Great Album", 10)
    assert not matches(rel("The_Artist-Other_Album-WEB-FLAC-2020"), "The Artist", "Great Album", 10)


def test_matches_ignores_spotify_edition_suffix() -> None:
    assert matches(rel("Artist-Album-WEB-FLAC-2009"), "Artist", "Album (Remastered 2009)", 10)


def test_matches_various_artists_skips_artist_check() -> None:
    assert matches(rel("VA-Summer_Hits-WEB-320-2020"), "Various Artists", "Summer Hits", 10)


def test_matches_rejects_discography_and_bad_sizes() -> None:
    assert not matches(rel("Artist-Discography-Album-FLAC"), "Artist", "Album", 10)
    assert not matches(rel("Artist-Album-FLAC", size=5 * MB), "Artist", "Album", 10)
    assert not matches(rel("Artist-Album-FLAC", size=5000 * MB), "Artist", "Album", 10)


def test_rank_orders_by_tier_then_grabs_and_skips_blocklist() -> None:
    releases = [
        rel("Artist-Album-320", guid="mp3", grabs=50),
        rel("Artist-Album-FLAC", guid="flac-few", grabs=1),
        rel("Artist-Album-FLAC-REPACK", guid="flac-many", grabs=9),
        rel("Artist-Album-FLAC-BAD", guid="blocked", grabs=99),
    ]
    ranked = rank(releases, "Artist", "Album", 10, blocklist={"blocked"})
    assert [r.guid for r in ranked] == ["flac-many", "flac-few", "mp3"]


def test_prowlarr_search_keeps_usenet_only() -> None:
    resp = MagicMock()
    resp.json.return_value = [
        {"guid": "a", "title": "A", "size": 1, "indexerId": 2, "downloadUrl": "u", "protocol": "usenet",
         "categories": [{"id": 3040, "name": "Audio/Lossless"}], "grabs": 3},
        {"guid": "b", "title": "B", "protocol": "torrent"},
    ]
    with patch("music_fetch.usenet.requests.get", return_value=resp) as get:
        releases = Prowlarr(url="http://p", api_key="k").search("Artist Album")
    assert [r.guid for r in releases] == ["a"]
    assert releases[0].categories == [3040]
    assert get.call_args.kwargs["headers"] == {"X-Api-Key": "k"}


def test_prowlarr_nzb_location_reads_redirect_without_following() -> None:
    resp = MagicMock(status_code=301, headers={"Location": "https://indexer/getnzb?id=1"})
    with patch("music_fetch.usenet.requests.get", return_value=resp) as get:
        assert Prowlarr(url="http://p", api_key="k").nzb_location(rel("x")) == "https://indexer/getnzb?id=1"
    assert get.call_args.kwargs["allow_redirects"] is False


def test_prowlarr_nzb_location_raises_without_redirect() -> None:
    resp = MagicMock(status_code=200, headers={})
    with patch("music_fetch.usenet.requests.get", return_value=resp):
        with pytest.raises(RuntimeError):
            Prowlarr(url="http://p", api_key="k").nzb_location(rel("x"))


def test_sabnzbd_add_url_returns_nzo_id() -> None:
    resp = MagicMock()
    resp.json.return_value = {"status": True, "nzo_ids": ["SABnzbd_nzo_1"]}
    with patch("music_fetch.usenet.requests.get", return_value=resp) as get:
        assert Sabnzbd(url="http://s", api_key="k").add_url("https://indexer/nzb", "Artist-Album") == "SABnzbd_nzo_1"
    params = get.call_args.kwargs["params"]
    assert params["mode"] == "addurl" and params["cat"] == "album" and params["name"] == "https://indexer/nzb"


def test_sabnzbd_add_url_raises_on_refusal() -> None:
    resp = MagicMock()
    resp.json.return_value = {"status": False, "error": "API Key Incorrect"}
    with patch("music_fetch.usenet.requests.get", return_value=resp):
        with pytest.raises(RuntimeError):
            Sabnzbd(url="http://s", api_key="k").add_url("u", "n")


def test_rank_prefers_the_spotify_year_within_a_tier() -> None:
    releases = [
        rel("Artist-Album-REMASTERED-FLAC-2015", guid="remaster", grabs=20),
        rel("Artist-Album-FLAC-1997", guid="original", grabs=2),
    ]
    assert [r.guid for r in rank(releases, "Artist", "Album", 10, set(), year=1997)] == ["original", "remaster"]


def test_sabnzbd_finished_keeps_completed_and_failed() -> None:
    resp = MagicMock()
    resp.json.return_value = {"history": {"slots": [
        {"nzo_id": "a", "status": "Completed", "storage": "/downloads/complete/album/A", "fail_message": ""},
        {"nzo_id": "b", "status": "Failed", "storage": "", "fail_message": "Repair failed"},
        {"nzo_id": "c", "status": "Extracting"},
    ]}}
    with patch("music_fetch.usenet.requests.get", return_value=resp) as get:
        done = Sabnzbd(url="http://s", api_key="k").finished(["a", "b", "c"])
    assert done == {
        "a": {"ok": True, "storage": "/downloads/complete/album/A", "fail_message": ""},
        "b": {"ok": False, "storage": "", "fail_message": "Repair failed"},
    }
    assert get.call_args.kwargs["params"]["nzo_ids"] == "a,b,c"


def test_sabnzbd_finished_empty_makes_no_call() -> None:
    with patch("music_fetch.usenet.requests.get") as get:
        assert Sabnzbd(url="http://s", api_key="k").finished([]) == {}
    get.assert_not_called()


# ---------------------------------------------------------------------------
# Wrong releases from the first dry-run sample (#189)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "artist, album, title, size_mb",
    [
        # Self-titled: the album words are the artist words.
        ("Wendy Eisenberg", "Wendy Eisenberg", "Wendy Eisenberg-Viewfinder-16BIT-WEB-FLAC-2024-ENRiCH", 541),
        # clean_album makes it self-titled; also a CD single.
        ("Electronic", "Electronic (Special Edition)", "Electronic-Vivid-CDRS6514-CDM-FLAC-1999-HOUND", 210),
        # The album name inside a box set's title.
        ("Bob Dylan", "Time Out Of Mind",
         "Bob Dylan-Fragments Time Out of Mind Sessions (1996-1997) The Bootleg Series Vol. 17-16BIT-WEB-FLAC-2023-ENRiCH",
         956),
        # A single of the album's title track.
        ("Social Distortion", "Born To Kill", "Social Distortion-Born To Kill-Single-24BIT-WEB-FLAC-2026-VEXED", 120),
    ],
)
def test_matches_rejects_the_wrong_picks(artist: str, album: str, title: str, size_mb: int) -> None:
    assert not matches(rel(title, size=size_mb * MB), artist, album, 11, album_type="album")


@pytest.mark.parametrize(
    "artist, album, title",
    [
        ("Wendy Eisenberg", "Wendy Eisenberg", "Wendy Eisenberg-Wendy Eisenberg-16BIT-WEB-FLAC-2024-ENRiCH"),
        ("Wendy Eisenberg", "Wendy Eisenberg", "Wendy Eisenberg-Self-Titled-WEB-FLAC-2024-GRP"),
        ("Electronic", "Electronic (Special Edition)", "Electronic-Electronic-(Special Edition)-2CD-FLAC-2013-GRP"),
        ("Bob Dylan", "Time Out Of Mind", "Bob Dylan-Time Out Of Mind-(Remastered)-WEB-FLAC-1997-GRP"),
        ("Social Distortion", "Born To Kill", "Social Distortion-Born To Kill-24BIT-WEB-FLAC-2026-VEXED"),
    ],
)
def test_matches_accepts_the_right_releases(artist: str, album: str, title: str) -> None:
    assert matches(rel(title), artist, album, 11, album_type="album")


@pytest.mark.parametrize(
    "artist, album, title",
    [
        # Picks from the first sample that the operator confirmed right.
        ("Tiga", "HOTLIFE", "Tiga-Hotlife-2026-24Bit-44.1kHz-FLAC"),
        ("Fred again..", "USB", "Fred Again.USB-SKUDERO-FLAC"),
        ("Jackson Mico Milas", "Blu Terra", "Jackson Mico Milas-Blu Terra-WEB-2022-BABAS"),
        ("JPEGMAFIA", "EXPERIMENTAL RAP", "JPEGMAFIA-EXPERIMENTAL RAP-24BIT-WEBFLAC-2026-NACHOS"),
        ("Danzig", "Danzig II: Lucifuge", "Danzig-II Lucifuge-CD-FLAC-1990-SCORN"),
        ("My New Band Believe", "My New Band Believe", "My New Band Believe-My New Band Believe-16BIT-WEB-FLAC-2026-FLACCiD"),
        ("Neurosis", "An Undying Love for a Burning World",
         "Neurosis-An Undying Love For A Burning World-24BIT-48KHZ-WEB-FLAC-2026-OBZEN"),
        ("Peter Broderick", "How They Are", "Peter Broderick-How They Are-2010-404"),
        ("Sam and Louise Sullivan", "Love & Devotion", "Sam and Louise Sullivan-Love and Devotion-16BIT-WEB-FLAC-2026-ENRiCH"),
        ("Man/Woman/Chainsaw", "Cannonball", "Man Woman Chainsaw-Cannonball-16BIT-WEB-FLAC-2026-FLACCiD"),
        ("Lusine", "The Waiting Room", "Lusine--The Waiting Room-GI-172-2013-OMA"),
        ("Erykah Badu", "Before The World Blows", "Erykah Badu X The Alchemist-Before The World Blows-16BIT-WEB-FLAC-2026-ENRiCH"),
        ("Jeffrey Lewis", "The EVEN MORE Freewheelin' Jeffrey Lewis",
         "Jeffrey Lewis-The Even More Freewheelin Jeffrey Lewis-16BIT-WEB-FLAC-2025-NRS"),
        ("Genesis Owusu", "REDSTAR WU & THE WORLDWIDE SCOURGE", "Genesis Owusu-Redstar Wu and The Worldwide Scourge-2026-FLAC"),
    ],
)
def test_matches_keeps_the_confirmed_picks(artist: str, album: str, title: str) -> None:
    assert matches(rel(title), artist, album, 0, album_type="album")


def test_matches_accepts_a_single_when_spotify_says_so() -> None:
    title = "Social Distortion-Born To Kill-Single-24BIT-WEB-FLAC-2026-VEXED"
    assert matches(rel(title, size=120 * MB), "Social Distortion", "Born To Kill", 2, album_type="single")
    # Without album_type, a short tracklist stands in for it.
    assert matches(rel(title, size=120 * MB), "Social Distortion", "Born To Kill", 2)
    assert not matches(rel(title, size=120 * MB), "Social Distortion", "Born To Kill", 11)


def test_matches_sizes_by_duration_when_known() -> None:
    forty_minutes = 40 * 60
    assert matches(rel("Artist-Album-WEB-FLAC-2020-GRP", size=300 * MB), "Artist", "Album", 10, seconds=forty_minutes)
    assert not matches(rel("Artist-Album-WEB-FLAC-2020-GRP", size=30 * MB), "Artist", "Album", 10, seconds=forty_minutes)
    assert not matches(rel("Artist-Album-WEB-FLAC-2020-GRP", size=3000 * MB), "Artist", "Album", 10, seconds=forty_minutes)


def test_rank_prefers_fewer_leftover_words_within_a_tier() -> None:
    releases = [
        rel("Artist-Album-Live Edit-WEB-FLAC-2020-GRP", guid="extra", grabs=50),
        rel("Artist-Album-WEB-FLAC-2020-GRP", guid="clean", grabs=1),
    ]
    assert [r.guid for r in rank(releases, "Artist", "Album", 10, set())] == ["clean", "extra"]


# ---------------------------------------------------------------------------
# Stylised names (#197)
# ---------------------------------------------------------------------------

GLYPH_ARTIST = "⣎⡇ꉺლ༽இ•̛)ྀ◞ ༎ຶ ༽ৣৢ؞ৢ؞ؖ ꉺლ"
GLYPH_TITLE = (
    "ʅ͡͡͡͡͡͡͡͡͡͡͡(̸̢̛̼̞̭͋ͅ)̸͚̰͛̔̾̀̿͒͂:̴͓̞̑̌̂̆̊͋̀:̸͎̟̯̂̓̌ ҉ ͡ ͞ ͞ ͞ ҉● ࿀ ● ࿀ ● ҉⃝ l̡̡̡ ̡͌ "
    "Ɵʅ͡͡͡͡͡͡͡͡͡͡͡(̸̢̛̼̞̭͋ͅ)̸͚̰͛̔̾̀̿͒͂v̴̢͚͚͎ȯ̶̞̮͖̑̈́)̸̳̥̰̜̥̺̐ͅ)̴͎̜͍̱̋̌͋̓̾̚ ̷̨ ☼⃝◞⊖◟ ∷፨◉☼⃝◞⊖◟☼⃝ꉂꆭ(☼⃝❁)ᕗ"
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Slayyyter WOR$T GIRL IN AMERICA", "slayyyter worst girl in america"),
        ("Tierra Whack WHACK'S MUSEUM", "tierra whack whacks museum"),
        ("Tierra Whack WHACK’S MUSEUM", "tierra whack whacks museum"),
        ("Beyoncé", "beyonce"),
        ("Sigur Rós Ágætis byrjun", "sigur ros agaetis byrjun"),
        ("P!nk", "pink"),
        ("Help!", "help"),
        ("Florence + the Machine", "florence and the machine"),
        ("Love & Devotion", "love and devotion"),
    ],
)
def test_normalise(text: str, expected: str) -> None:
    assert normalise(text) == expected


@pytest.mark.parametrize("text", ["WOR$T GIRL IN AMERICA", "Beyoncé", "Fred again..", "On", "Danzig II: Lucifuge"])
def test_readable(text: str) -> None:
    assert readable(text)


@pytest.mark.parametrize("text", [GLYPH_ARTIST, GLYPH_TITLE, "÷", "!!!", ""])
def test_glyph_names_are_not_readable(text: str) -> None:
    assert not readable(text)


@pytest.mark.parametrize(
    "artist, album, title",
    [
        ("Slayyyter", "WOR$T GIRL IN AMERICA", "Slayyyter-WORsT GIRL IN AMERICA-24BIT-48KHZ-WEB-FLAC-2026-OBZEN"),
        ("Slayyyter", "WOR$T GIRL IN AMERICA", "Slayyyter-Worst Girl In America-CD-FLAC-2026-PERFECT"),
        ("Tierra Whack", "WHACK'S MUSEUM", "Tierra Whack-WHACKS MUSEUM-24BIT-WEB-FLAC-2026-ENRiCH"),
        ("Beyoncé", "RENAISSANCE", "Beyonce-Renaissance-24BIT-WEB-FLAC-2022-ENRiCH"),
        ("Sigur Rós", "Ágætis byrjun", "Sigur Ros-Agaetis Byrjun-CD-FLAC-1999-GRP"),
    ],
)
def test_matches_stylised_names(artist: str, album: str, title: str) -> None:
    assert matches(rel(title, size=500 * MB), artist, album, 10)


def test_matches_override_words() -> None:
    """An override is matched as the album; its words stand in for the artist (#200)."""
    assert matches(rel("webdings-four-tet", size=300 * MB), "", "webdings four tet", 10)
    assert not matches(rel("four-tet-rounds", size=300 * MB), "", "webdings four tet", 10)


def test_matches_any_album_by_the_artist_on_size_and_type() -> None:
    """A title with no words matches any album-sized release by the artist."""
    assert matches(rel("Artist-Some Album-WEB-FLAC-2026-GRP", size=300 * MB), "Artist", "÷", 10, any_album=True)
    assert not matches(rel("Other-Some Album-WEB-FLAC-2026-GRP", size=300 * MB), "Artist", "÷", 10, any_album=True)
    assert not matches(rel("Artist-Some Song-Single-WEB-FLAC-2026-GRP", size=300 * MB), "Artist", "÷", 10,
                       any_album=True, album_type="album")
    assert not matches(rel("Artist-Some Album-WEB-FLAC-2026-GRP", size=5 * MB), "Artist", "÷", 10, any_album=True)


# ---------------------------------------------------------------------------
# The release title leads with the artist (#200)
# ---------------------------------------------------------------------------


def test_matches_rejects_another_artists_album_of_the_same_name() -> None:
    """Elvis27's album *Electronic* is not the band Electronic's self-titled one:
    the digit-bearing artist word must not vanish into the year/catalogue rule."""
    title = "Elvis27-Electronic-ANTI087-WEB-2026-YALLA"
    assert not matches(rel(title, size=171 * MB), "Electronic", "Electronic (Special Edition)", 11,
                       album_type="album")


def test_leftover_counts_digit_words_only_in_the_artist_segment() -> None:
    title = "Artist X Elvis27-Album-ANTI087-WEB-2026-GRP"
    assert leftover(rel(title), "Artist", "Album") == ["x", "elvis27"]


def test_matches_needs_the_artist_first() -> None:
    assert not matches(rel("Great Album-The Artist-WEB-FLAC-2020-GRP"), "The Artist", "Great Album", 10)
    assert not matches(rel("Someone-Summer Hits-WEB-320-2020-GRP"), "Various Artists", "Summer Hits", 10)
    assert matches(rel("Various Artists-Summer Hits-WEB-320-2020-GRP"), "Various Artists", "Summer Hits", 10)
    assert matches(rel("Jay-Z-The Blueprint-CD-FLAC-2001-GRP"), "JAY-Z", "The Blueprint", 13)
    assert matches(rel("Beatles-Abbey Road-CD-FLAC-1969-GRP"), "The Beatles", "Abbey Road", 17)


@pytest.mark.parametrize(
    "artist, album, title, size_mb",
    [
        # Every pick from the 2026-10-05 re-sample (Loki, from 12:50 UTC), Electronic aside.
        ("Danzig", "Danzig II: Lucifuge", "Danzig-II Lucifuge-CD-FLAC-1990-SCORN", 354),
        ("Jeffrey Lewis", "The EVEN MORE Freewheelin' Jeffrey Lewis",
         "Jeffrey Lewis-The Even More Freewheelin Jeffrey Lewis-16BIT-WEB-FLAC-2025-NRS", 300),
        ("Gold Panda", "TON UP", "Gold Panda-TON UP-16BIT-WEB-FLAC-2026-ENRiCH", 251),
        ("Genesis Owusu", "REDSTAR WU & THE WORLDWIDE SCOURGE",
         "Genesis Owusu-Redstar Wu and The Worldwide Scourge-2026-FLAC", 413),
        ("mary in the junkyard", "Role Model Hermit",
         "Mary In The Junkyard-Role Model Hermit-24BIT-WEB-FLAC-2026-ENViED", 578),
        ("Swapmeet", "Mount Zero", "Swapmeet-Mount Zero-24BIT-WEB-FLAC-2026-ENRiCH", 465),
        ("My New Band Believe", "My New Band Believe",
         "My New Band Believe-My New Band Believe-16BIT-WEB-FLAC-2026-FLACCiD", 305),
        ("Neurosis", "An Undying Love for a Burning World",
         "Neurosis-An Undying Love For A Burning World-24BIT-48KHZ-WEB-FLAC-2026-OBZEN", 942),
        ("Aldous Harding", "Train on the Island", "Aldous Harding-Train On The Island-24BIT-WEB-FLAC-2026-ENRiCH", 901),
        ("Bob Dylan", "Time Out Of Mind", "Bob Dylan-Time Out Of Mind-24-44-WEB-FLAC-REMASTERED-1997-OBZEN", 856),
        ("IAN SWEET", "Shiverstruck", "IAN SWEET-Shiverstruck-24BIT-WEB-FLAC-2026-ENRiCH", 928),
        ("Sam and Louise Sullivan", "Love & Devotion",
         "Sam and Louise Sullivan-Love and Devotion-16BIT-WEB-FLAC-2026-ENRiCH", 216),
        ("Man/Woman/Chainsaw", "Cannonball", "Man Woman Chainsaw-Cannonball-16BIT-WEB-FLAC-2026-FLACCiD", 330),
        ("Lusine", "The Waiting Room", "Lusine--The Waiting Room-GI-172-2013-OMA", 113),
        ("Erykah Badu", "Before The World Blows",
         "Erykah Badu X The Alchemist-Before The World Blows-16BIT-WEB-FLAC-2026-ENRiCH", 453),
        ("Lambchop", "Punching the Clown", "Lambchop-Punching the Clown-WEB-2026-ENRiCH", 127),
        ("Sports Team", "Boys These Days",
         "Sports Team-Boys These Days-DELUXE EDITION-24BIT-44KHZ-WEB-FLAC-2025-OBZEN", 939),
        ("Tierra Whack", "WHACK'S MUSEUM", "Tierra Whack-WHACKS MUSEUM-16BIT-WEB-FLAC-2026-ENRiCH", 185),
        ("Slayyyter", "WOR$T GIRL IN AMERICA", "Slayyyter-Worst Girl In America-CD-FLAC-2026-PERFECT", 401),
        ("L'Rain", "fata morgana", "LRain-fata morgana-16BIT-WEB-FLAC-2026-ENRiCH", 253),
        # A compilation.
        ("Various Artists", "Summer Hits", "VA-Summer Hits-WEB-FLAC-2026-GRP", 400),
    ],
)
def test_matches_keeps_the_resample_picks(artist: str, album: str, title: str, size_mb: int) -> None:
    assert matches(rel(title, size=size_mb * MB), artist, album, 0)


def test_matches_override_needs_its_words_first() -> None:
    """An override (no artist) still checks the title's first word against its words."""
    title = "Wingdings-Four Tet-Untitled-TEXT059-WEB-2026-BB"
    assert matches(rel(title, size=102 * MB), "", "wingdings four tet", 0)
    assert not matches(rel("Elvis27-Wingdings Four Tet-WEB-2026-GRP", size=102 * MB), "", "wingdings four tet", 0)

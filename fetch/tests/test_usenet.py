"""Tests for music_fetch.usenet — release matching, ranking and the grab redirect."""

from unittest.mock import MagicMock, patch

import pytest

from music_fetch.usenet import Prowlarr, Release, Sabnzbd, clean_album, matches, rank, tier

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

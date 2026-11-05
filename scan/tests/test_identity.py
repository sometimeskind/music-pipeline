"""Tests for music_scan.identity and the library's use of spotify_ids."""

from unittest.mock import MagicMock

from music_scan.identity import (
    BY_ID,
    BY_ISRC,
    BY_WORDS,
    ItemIndex,
    Recordings,
    add_isrcs,
    add_to_list,
    item_spotify_ids,
    recording_ids,
    release_match,
    spotify_id,
    split_list,
)


def _item(title="Song", **data):
    item = MagicMock(title=title, artist="Artist", albumartist="Artist")
    item.get = lambda k, d=None: data.get(k, d)
    item.__setitem__ = lambda _, k, v: data.__setitem__(k, v)
    item.data = data
    return item


def test_spotify_id_from_track_url() -> None:
    assert spotify_id("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC") == "4uLU6hMCjMI75M1A2tKUQC"
    assert spotify_id("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc") == "4uLU6hMCjMI75M1A2tKUQC"


def test_spotify_id_rejects_other_urls() -> None:
    assert spotify_id(None) is None
    assert spotify_id("") is None
    assert spotify_id("https://open.spotify.com/album/1") is None
    assert spotify_id("https://www.youtube.com/watch?v=x") is None


def test_split_list_drops_blanks() -> None:
    assert split_list(" a, ,b,") == ["a", "b"]
    assert split_list(None) == []
    assert split_list("X;Y", sep=";") == ["X", "Y"]


def test_add_to_list_appends_once() -> None:
    data: dict = {}
    item = MagicMock()
    item.get = lambda k, d=None: data.get(k, d)
    item.__setitem__ = lambda _, k, v: data.__setitem__(k, v)

    assert add_to_list(item, "spotify_ids", "A")
    assert add_to_list(item, "spotify_ids", "B")
    assert not add_to_list(item, "spotify_ids", "A")
    assert not add_to_list(item, "spotify_ids", None)
    assert data["spotify_ids"] == "A,B"


def test_item_spotify_ids_includes_legacy_spotify_url() -> None:
    item = _item(spotify_ids="A,B", spotify_url="https://open.spotify.com/track/C")
    assert item_spotify_ids(item) == {"A", "B", "C"}


def test_add_isrcs_unions_musicbrainz_and_spotify() -> None:
    item = _item(isrc="USX1;GBX2")
    assert add_isrcs(item, ["GBX2", "SEX3"])
    assert item.data["isrc"] == "USX1;GBX2;SEX3"
    assert not add_isrcs(item, ["USX1", None])


def test_item_index_ladder() -> None:
    by_id = _item("Song", spotify_ids="SID")
    by_isrc = _item("Song (Remastered)", isrc="A;B")
    by_words = _item("Other")
    index = ItemIndex([by_id, by_isrc, by_words])

    assert index.match("SID", "B", "Song", "Artist") == (by_id, BY_ID)
    assert index.match("NOPE", "B", "Song", "Artist") == (by_isrc, BY_ISRC)
    assert index.match(None, None, "Other", "Artist") == (by_words, BY_WORDS)
    assert index.match(None, None, "Other", "Artist", words=False) == (None, None)
    assert index.match_song({"name": "x", "url": "https://open.spotify.com/track/SID"}) == (by_id, BY_ID)
    assert dict(index.rungs) == {BY_ID: 2, BY_ISRC: 1, BY_WORDS: 1}



def test_words_rung_skips_identified_items() -> None:
    """The live take holds its own ID: it never fills the studio entry by words (#257)."""
    live = _item("Girlfriend Is Better", spotify_ids="LIVE")
    index = ItemIndex([live])
    assert index.match("STUDIO", "USSTUDIO", "Girlfriend Is Better", "Artist") == (None, None)
    assert index.match("LIVE", None, "Girlfriend Is Better", "Artist") == (live, BY_ID)
    idless = _item("Girlfriend Is Better")
    assert ItemIndex([live, idless]).match("STUDIO", None, "Girlfriend Is Better", "Artist") == (idless, BY_WORDS)


# --- release_match (#240) -----------------------------------------------------


def _credit(title, artist, albumartist=""):
    return MagicMock(title=title, artist=artist, albumartist=albumartist)


def test_release_match_reads_through_feat_credits_and_curly_apostrophes() -> None:
    feat = _credit("Kiss Me", "CFCF feat. nuum & Seren Forever")
    curly = _credit("Marvin\u2019s Room", "Drake")
    assert release_match("Kiss Me", "CFCF", [feat]) is feat
    assert release_match("Marvins Room", "Drake", [curly]) is curly
    assert release_match("Lord Knows", "Drake", [_credit("Lord Knows", "Rick Ross", "Drake")]) is not None


def test_release_match_needs_the_same_title_and_the_artist() -> None:
    euro = _credit("Kiss Me (Euroversion)", "CFCF")
    assert release_match("Kiss Me", "CFCF", [euro]) is None
    assert release_match("Kiss Me - Euroversion", "CFCF", [euro]) is euro
    assert release_match("Kiss Me", "Someone Else", [_credit("Kiss Me", "CFCF")]) is None


# --- remaster notes (#243, #252) ----------------------------------------------------


def test_a_remaster_is_another_track() -> None:
    """Title words never make two masters one track (#252): a remaster has its own ISRCs."""
    item = _credit("Miserabilia", "Los Campesinos!")
    assert release_match("Miserabilia (2018 Remaster)", "Los Campesinos!", [item]) is None
    assert release_match("Miserabilia", "Los Campesinos!", [_credit("Miserabilia (2018 Remaster)", "Los Campesinos!")]) is None
    index = ItemIndex([_item("Miserabilia")])
    assert index.match("NOPE", "NOPE", "Miserabilia (2018 Remaster)", "Artist") == (None, None)
    remaster = _item("Miserabilia (2018 Remaster)", isrc="GBREM1800001")
    assert ItemIndex([remaster]).match(None, "GBREM1800001", "Miserabilia (Best Of)", "Artist") == (remaster, BY_ISRC)


def test_recordings_hold_a_mb_recording_only_at_its_length() -> None:
    """homelab#2540: 6994 (the 5.1 mix, 456 s) holds the recording beets guessed for
    the stereo 6930 (445 s); a file of the stereo is not held by it."""
    library = Recordings([_item(mb_trackid="d0063aee", length=456.0)])
    assert not library.holds(recording_ids(mb_trackid="d0063aee"), 445.2)
    assert library.holds(recording_ids(mb_trackid="d0063aee"), 455.0)
    assert library.holds(recording_ids(mb_trackid="d0063aee"))  # no length to veto with
    assert not library.holds_item(_item(mb_trackid="d0063aee", length=445.2))
    # A Spotify ID or ISRC holds a recording whatever the lengths.
    library = Recordings([_item(isrc="USX1", mb_trackid="d0063aee", length=456.0)])
    assert library.holds(recording_ids(isrcs=["USX1"], mb_trackid="d0063aee"), 445.2)


def test_recordings_hold_a_recording_by_spotify_id_isrc_or_mb_recording_id_never_by_title() -> None:
    """The identity rule of homelab#1889 (#287)."""
    library = Recordings([_item(spotify_ids="SP1"), _item(isrc="usx1;GBX2"), _item(mb_trackid="ABC-REC")])
    assert library.holds(recording_ids(["SP1"]))
    assert library.holds(recording_ids(isrcs=[" usx1 "]))
    assert library.holds(recording_ids(mb_trackid="abc-rec"))
    assert library.holds_item(_item(title="Other", isrc="GBX2"))
    assert not library.holds_item(_item(title="Song"))  # the same title is not the same recording
    assert not library.holds(recording_ids(["SP2"], ["USX9"], "other-rec"))
    assert not Recordings().holds(recording_ids(["SP1"]))


def test_merge_agrees_needs_the_title_and_a_shared_artist_word() -> None:
    """homelab#2539: a merged file counts as its keeper's recording only when its tags agree."""
    from music_scan.identity import merge_agrees

    assert merge_agrees("Love Cry (Joy Orbison remix)", "Four Tet", "Love Cry", "Four Tet")
    assert merge_agrees("Ava Adore", "", "Ava Adore", "The Smashing Pumpkins")  # unknown artist doesn't veto
    assert merge_agrees("Heaven", "Talking Heads", "Heaven - 2005 Remaster", "Talking Heads feat. David Byrne")
    assert not merge_agrees("Pictures Of You - Remastered", "The Cure", "Easy on Your Own?", "Alvvays")
    assert not merge_agrees("Heaven", "Talking Heads", "Heaven", "Bryan Adams")
    assert not merge_agrees("", "Alvvays", "Easy on Your Own?", "Alvvays")


def test_recordings_holds_a_merged_name_only_with_an_agreeing_keeper() -> None:
    class Item(dict):
        id = property(lambda self: self["id"])

    lib = Recordings([Item(id=9673, title="Easy on Your Own?", artist="Alvvays",
                           merged_files="451-02 - x.m4a/9674-02 - y.m4a")])
    assert lib.holds_merged("9674-02 - y.m4a", "Easy on Your Own?", "Alvvays")
    assert not lib.holds_merged("451-02 - x.m4a", "Pictures Of You - Remastered", "The Cure")
    assert not lib.holds_merged("unknown.m4a", "Easy on Your Own?", "Alvvays")
    assert [k.id for k in lib.merge_keepers("451-02 - x.m4a")] == [9673]


def test_different_song_reads_titles_without_their_version_notes() -> None:
    """homelab#2544: a sibling recording is the same song; a venue suffix doesn't hide another song."""
    from music_scan.identity import different_song

    assert not different_song("Oh! Sweet Nuthin'", "Oh! Sweet Nuthin' (5.1 mix)")
    assert not different_song("Song (Live at X, 1975)", "Song")
    assert not different_song("Song - 2015 Remaster", "Song [mono]")
    assert not different_song("Its Alright", "It's Alright")  # apostrophes are folded
    assert different_song("Romance in Durango (Live at Harvard Square Theatre, Cambridge, MA - November 1975)",
                          "It Ain't Me, Babe (Live at Harvard Square Theatre, Cambridge, MA - November 1975)")
    # Release tags misspell: one song still (Illinois, homelab#2544).
    assert not different_song("In This Temple as in the Hearts of Man for Whom He Saved theEarth",
                              "In This Temple as in the Hearts of Man for Whom He Saved the Earth")
    assert not different_song("They Are Night Zombies!! They Are Neighbors!! They Have ComeBack fromthe Dead!! Ahhhh!",
                              "They Are Night Zombies!! They Are Neighbors!! They Have Come Back From the Dead!! Ahhhh!")
    assert not different_song("To the Workers of the Rock River Valley Region, I Have an Idea Concerning Your Predicament",
                              "To the Workers of the Rockford River Valley Region, I Have an Idea Concerning Your Predicament")
    # Short titles a letter or two apart are two songs.
    assert different_song("Intro", "Outro")
    assert different_song("Amy", "Ama")
    # A credit in the title is not the song's name (Ironman, The Velvet Rope, homelab#2544).
    assert not different_song("Fish feat Raekwon & Cappadonna", "Fish")
    assert not different_song("Daytona 500 Ft. Raekwon", "Daytona 500")
    assert not different_song("got til it's gone feat. q-tip and joni mitchell", "Got ’til It’s Gone")
    assert not different_song("Song Featuring Someone (Live)", "Song (Live at X)")
    assert different_song("Fish feat Raekwon & Cappadonna", "Camay")
    assert different_song("Feat Of Clay", "Song")  # a title that opens with the word keeps it
    # Nothing to compare by: never two songs.
    assert not different_song("恋人", "Koibito")
    assert not different_song("", "Song")


def test_version_clash_names_a_version_only_one_side_names() -> None:
    """homelab#2609: Nebraska '82's live takes against the studio recordings beets chose,
    and the reverse; a live album's bare titles are covered by its name."""
    from music_scan.identity import version_clash

    assert version_clash("Reason To Believe (Nebraska Live)", "Reason to Believe", b_notes="version 1b") == ["live"]
    red_bank = "live, 2025‐04‐22: Hackensack Meridian Health Theatre, Red Bank, NJ, USA"
    assert version_clash("My Father's House", "My Father’s House", b_notes=red_bank,
                         a_album="Nebraska '82: Expanded Edition") == ["live"]
    assert version_clash("My Father's House", "My Father's House (Nebraska Live)") == ["live"]
    assert version_clash("Oh! Sweet Nuthin'", "Oh! Sweet Nuthin' (5.1 mix)", b_notes="5.1 mix") == ["5.1"]
    # The same version on both sides, in a title or the notes.
    assert version_clash("Song (Live)", "Song", b_notes="live, 1975-11-20") == []
    assert version_clash("Song (Live)", "Song (Live)", b_notes="live, 1975-11-20") == []
    assert version_clash("Live Forever (Live)", "Live Forever", b_notes="live") == []
    # A song whose name holds the word, against the same name.
    assert version_clash("Live Forever", "Live Forever") == []
    assert version_clash("Take Five", "Take Five") == []
    # A live album's bare title: its album name covers the recording's "live".
    assert version_clash("Substitute", "Substitute", b_notes="live, 1970-02-14: Leeds", a_album="Live at Leeds") == []
    assert version_clash("Substitute", "Substitute", b_notes="live, 1970-02-14: Leeds") == ["live"]
    # Not a version word: a remaster, a "version 1b".
    assert version_clash("Song - 2015 Remaster", "Song", b_notes="version 1b") == []


def _artists(*names: str, ids=()):
    from music_scan.identity import Credit

    return Credit(names, frozenset(ids))


def test_different_artist_refutes_two_names_that_share_nothing() -> None:
    """homelab#2649: 18518, Springsteen's I'm on Fire matched to 5000 Volts' song."""
    from music_scan.identity import different_artist

    assert different_artist(_artists("Bruce Springsteen"), _artists("5000 Volts"))
    assert different_artist(_artists("The Who"), _artists("The Band"))  # "the" alone is no shared word
    assert different_artist(_artists("Simon & Garfunkel"), _artists("Sonny & Cher"))  # nor "&", which reads "and"
    assert different_artist(_artists("A Tribe Called Quest feat. Busta"), _artists("Of Montreal with Friends"))


def test_different_artist_passes_credits_of_one_artist() -> None:
    from music_scan.identity import different_artist

    for a, b in [("Bruce Springsteen & The E Street Band", "Bruce Springsteen"),  # 18513, a band credit
                 ("Bruce Springsteen", "Springsteen, Bruce"), ("Springsteen Bruce", "BRUCE SPRINGSTEEN"),
                 ("CFCF feat. nuum & Seren Forever", "CFCF"), ("Beatles", "The Beatles"), ("Björk", "Bjork"),
                 ("AC/DC", "ACDC"), ("R.E.M.", "REM"), ("Ke$ha", "Kesha"), ("Sergei Rachmaninoff", "Sergey Rachmaninov"),
                 ("Tchaikovsky", "Chaikovsky"), ("BruceSpringsteen & E Street Band", "Bruce Springsteen")]:
        assert not different_artist(_artists(a), _artists(b)), (a, b)


def test_different_artist_keeps_what_words_cant_read() -> None:
    from music_scan.identity import different_artist

    for a, b in [("", "5000 Volts"), ("Bruce Springsteen", ""), ("Various Artists", "5000 Volts"), ("VA", "5000 Volts"),
                 ("V.A.", "5000 Volts"), ("5000 Volts", "Various"), ("[unknown]", "5000 Volts"),
                 ("Ryuichi Sakamoto", "坂本龍一"), ("Kino", "Кино"), ("БИ-2", "Bi2"), ("Cui Jian 崔健", "Tsui Chien"),
                 ("The The", "The Cure"), ("!!!", "The Cure")]:
        assert not different_artist(_artists(a), _artists(b)), (a, b)
    assert not different_artist(_artists(), _artists("5000 Volts"))


def test_different_artist_one_alike_name_or_id_is_enough() -> None:
    from music_scan.identity import different_artist

    assert not different_artist(_artists("Clarence Clemons", "Bruce Springsteen"), _artists("Bruce Springsteen"))
    assert not different_artist(_artists("Yusuf", ids=["a"]), _artists("Cat Stevens", ids=["a", "b"]))
    assert different_artist(_artists("Yusuf", ids=["a"]), _artists("Cat Stevens", ids=["b"]))


def test_artist_credit_reads_an_item_and_a_matchs_info() -> None:
    from beets.library import Item

    from music_scan.identity import artist_credit

    item = Item(artist="Bruce Springsteen & The E Street Band", artists=["Bruce Springsteen", "The E Street Band"],
                mb_artistid="a", mb_artistids=["a", "b"], albumartist="Bruce Springsteen", mb_albumartistid="a")
    assert artist_credit(item).names == ("Bruce Springsteen & The E Street Band", "Bruce Springsteen", "The E Street Band")
    assert artist_credit(item).ids == {"a", "b"}
    assert artist_credit(item, album=True).names == ("Bruce Springsteen",)
    info = {"artist": "5000 Volts", "artist_credit": "5000 Volts", "artists": ["5000 Volts"], "artist_id": "v",
            "artists_ids": ["v"]}
    assert artist_credit(info).names == ("5000 Volts",) and artist_credit(info).ids == {"v"}


def test_compilation_reads_the_flag_the_type_and_the_album_artist() -> None:
    from beets.library import Item

    from music_scan.identity import compilation

    assert compilation(Item(comp=True))
    assert compilation(Item(albumartist="Various Artists"))
    assert compilation(Item(albumartist="V.A."))
    assert compilation(Item(albumtypes=["album", "compilation"]))
    assert not compilation(Item(albumartist="Bruce Springsteen", albumtypes=["album"]))
    assert not compilation(Item())


def test_merge_agrees_takes_no_shared_word_from_the() -> None:
    """The word rule is artist_words' (homelab#2649): "the" links no two artists."""
    from music_scan.identity import merge_agrees

    assert not merge_agrees("Heaven", "The Cure", "Heaven", "The Smiths")
    assert merge_agrees("Heaven", "The Cure", "Heaven", "Cure")

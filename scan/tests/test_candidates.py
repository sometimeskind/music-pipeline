"""recording_candidates (homelab#2623), against MusicBrainz responses recorded on 2026-11-03.

``mb_responses/`` holds the releases of the three items the connector stopped on
(13744, 3809, 17061), the release-group listing and the fitting variants the
17061 lookup reads.  The expected picks are the issue's table, found by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from music_scan import candidates as cand

RESPONSES = Path(__file__).resolve().parent / "mb_responses"


class Item(dict):
    def __init__(self, id_, **fields):
        super().__init__(fields)
        self.id = id_


def recorded(calls=None):
    """A MusicBrainz get over the recorded responses; None (a 404) for one not recorded."""
    def get(path, **params):
        if calls is not None:
            calls.append((path, params))
        if path == "release":
            name = f"releases-of-group-{params['release-group'][:8]}"
        else:
            kind, mbid = path.split("/")
            name = f"{kind}-{mbid[:8]}"
        f = RESPONSES / f"{name}.json"
        return json.loads(f.read_text()) if f.exists() else None
    return get


LOADED = Item(13744, title="Oh! Sweet Nuthin' (2015 Remaster)", artist="The Velvet Underground", disc=0, track=10,
              tracktotal=95, length=445.21, mb_albumid="c4f65037-c685-48ab-89b6-af226335b964",
              mb_trackid="d0063aee-08fe-4b9c-9017-6fe09418af08", isrc="")
GOLD_RUSH = Item(3809, title="After the Gold Rush", artist="Neil Young", disc=1, track=2, tracktotal=14, length=226.89,
                 mb_albumid="67101825-7368-4d64-a081-f35e294bde5b", mb_trackid="0ef25c6b-c082-4750-8b02-e27fe7c121a3")
FATHERS_HOUSE = Item(17061, title="My Father’s House", artist="Bruce Springsteen", disc=4, track=9, tracktotal=10,
                     disctotal=4, length=307.48, mb_albumid="ee7b8cb1-a1c6-41a8-8f5c-3bc9dd9b8d21",
                     mb_releasegroupid="324aee0c-36ec-35c1-9c93-b67a1d428037",
                     mb_trackid="4cb4128a-0e58-4b53-a04f-3551c7292f76", isrc="USSM12503412")


@pytest.mark.parametrize("item, at_position", [
    (LOADED, {"5019cf99-4659-4b09-bd19-dd8d31badfac"}),
    (GOLD_RUSH, {"a1d14a11-b6ec-4dd9-a8f8-c58deb43a41f"}),
    # A variant: the item's disc 4 isn't on its one-disc release; the 2025 CDs have the
    # issue's pick there, the digital release another take.
    (FATHERS_HOUSE, {"ab727990-c905-4b2d-bcfd-f2f991c2d7ef", "9be6df53-1e1f-4d31-be2d-1f526cba533b"}),
])
def test_the_issues_items_get_the_recording_at_their_position(item, at_position, caplog):
    caplog.set_level("INFO")
    found = cand.recording_candidates(item, recorded())
    assert {c.recording for c in found if c.at_position} == at_position
    assert f"--set-recording {item.id} <MBID>" in caplog.text


def test_loaded_lists_the_siblings_with_the_current_one_marked(caplog):
    caplog.set_level("INFO")
    found = {c.recording[:8]: c for c in cand.recording_candidates(LOADED, recorded())}
    # The stereo remaster at disc 1 #10 (447 s against the file's 445 s), the mono, the early
    # version, the live take and the 5.1 mix the item holds now (disc 6, twice).
    assert set(found) == {"5019cf99", "a0e58c05", "dfff014f", "45971683", "d0063aee"}
    assert found["5019cf99"].at == ["disc 1 #10", "disc 6 #30"]
    assert found["5019cf99"].isrcs == ("USAT20103144", "USRH11504973")
    assert found["d0063aee"].at == ["disc 6 #10", "disc 6 #20"] and not found["d0063aee"].at_position
    assert "the item has no disc number: track 10 is counted across the release" in caplog.text
    assert "'Oh! Sweet Nuthin’ (5.1 mix)' — 446.0 s, +0.8 s against the file" in caplog.text
    assert caplog.text.count("← current") == 1
    assert "[VARIANT]" not in caplog.text


def test_gold_rush_says_the_current_recording_is_not_on_the_release(caplog):
    caplog.set_level("INFO")
    [only] = cand.recording_candidates(GOLD_RUSH, recorded())
    assert (only.title, only.disambiguation, only.length) == ("After the Gold Rush", "original studio mix", 226066)
    assert "[CURRENT] the item's recording 0ef25c6b-c082-4750-8b02-e27fe7c121a3 is on none" in caplog.text


def test_a_variant_is_read_from_the_release_group(caplog):
    caplog.set_level("INFO")
    calls = []
    found = {c.recording[:8]: c for c in cand.recording_candidates(FATHERS_HOUSE, recorded(calls))}
    # The item's release, the group's listing, and the three best fitting variants: digital first.
    assert [p for p, _ in calls] == ["release/ee7b8cb1-a1c6-41a8-8f5c-3bc9dd9b8d21", "release",
                                     "release/f555867c-827e-4c0f-8de3-af4fad9f18b9",
                                     "release/3905d186-154c-4ff9-8f09-58c56a25fcf0",
                                     "release/5c56fd15-3aea-48fc-9ba8-ee91589df3ca"]
    assert calls[1][1] == {"release-group": "324aee0c-36ec-35c1-9c93-b67a1d428037", "inc": "media", "limit": "100"}
    assert found["ab727990"].at == ["disc 4 #9 on 3905d186 (CD/Blu-ray, US 2025-10-24)",
                                    "disc 4 #9 on 5c56fd15 (CD/Blu-ray, XE 2025-10-24)"]
    assert found["ab727990"].length == 307466  # the file is 307.48 s
    assert found["4cb4128a"].disambiguation.startswith("live, 2025")  # the take it holds now
    assert found["4cb4128a"].isrcs == ("USSM12503412",)  # the ISRC the file is tagged with
    assert "de4df851" in found  # the item's own release's take, at disc 1 #9
    assert "[VARIANT] the item's disc 4 track 9" in caplog.text
    assert "[NOTE] nothing on the item's release sits at disc 4 track 9" in caplog.text


def test_no_release_is_refused():
    with pytest.raises(SystemExit, match="has no mb_albumid"):
        cand.recording_candidates(Item(1, title="T", mb_albumid=""), recorded())
    with pytest.raises(SystemExit, match="MusicBrainz has no release"):
        cand.recording_candidates(Item(1, title="T", mb_albumid="00000000-0000-0000-0000-000000000000"), recorded())


def _release(*discs):
    return {"media": [{"position": n, "track-count": len(ids), "tracks": [
        {"id": f"t{n}-{p}", "position": p, "recording": {"id": rid, "title": "Song"}}
        for p, rid in enumerate(ids, start=1)]} for n, ids in enumerate(discs, start=1)]}


def test_position_and_fit():
    rel = _release(["a", "b"], ["c", "d", "e"])
    assert cand.position_track(rel, 2, 3)["recording"]["id"] == "e"
    assert cand.position_track(rel, 0, 3)["recording"]["id"] == "c"  # no disc: counted across the release
    assert cand.position_track(rel, 3, 1) is None and cand.position_track(rel, 1, 0) is None
    assert cand.fits(rel, 2, 3, 3) and cand.fits(rel, 2, 3, 5) and cand.fits(rel, 2, 3, 0)
    assert not cand.fits(rel, 2, 3, 10) and not cand.fits(rel, 3, 1, 0) and not cand.fits(rel, 1, 3, 0)

"""Rolling back failed Usenet releases' unmatched tracks (#238)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from music_scan.identity import ItemIndex
from music_scan.rollback import entry_keys, failed_release_items, links, quarantine, rollback_release, unmatched

GRAB = datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc)
IMPORT = datetime(2026, 10, 7, 21, 40, tzinfo=timezone.utc)


class FakeItem:
    """Enough of a beets Item: ``get`` sees every field, ``remove`` records the call."""

    def __init__(self, id_, added, **data):
        self.id, self.added, self.data = id_, added, data
        self.title, self.artist = data["title"], data["artist"]
        self.albumartist = data.get("albumartist", "")
        self.path = f"/lib/{data['artist']}/{data['title']}-{id_}.m4a".encode()
        self.removed = False

    def get(self, key, default=None):
        return self.data.get(key, default)

    def remove(self, delete=False):
        assert delete is False, "a rollback never deletes files"
        self.removed = True


def item(id_, title, artist="Talking Heads", added=0.0, **data) -> FakeItem:
    return FakeItem(id_, added, title=title, artist=artist, **data)


def _on_disk(tmp_path: Path, i: FakeItem) -> FakeItem:
    path = tmp_path / "library" / i.path.decode().lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    i.path = str(path).encode()
    return i


def spotdl(tmp_path: Path, name: str, songs: list[dict]) -> Path:
    (tmp_path / f"{name}.spotdl").write_text(json.dumps({"songs": songs}))
    return tmp_path


def record(status, name="Remain in Light (Deluxe Version)", artist="Talking Heads", **extra) -> dict:
    return {"status": status, "name": name, "artist": artist,
            "grabbed_at": GRAB.isoformat(), "imported_at": IMPORT.isoformat(), **extra}


def test_entry_keys_cover_every_playlist(tmp_path) -> None:
    spotdl(tmp_path, "later", [{"song_id": "s1", "isrc": "GB1"}, {"url": "https://open.spotify.com/track/s2"}])
    spotdl(tmp_path, "keep", [{"song_id": "s3"}])
    assert entry_keys(tmp_path) == ({"s1", "s2", "s3"}, {"GB1"})


def test_unmatched_by_spotify_id_or_isrc_only() -> None:
    by_id = item(1, "Once in a Lifetime", spotify_ids="s1")
    by_isrc = item(2, "Houses in Motion", isrc="US1;GB1")
    outtake = item(3, "Fela's Riff (Unfinished Outtake)", isrc="US9")
    assert unmatched([by_id, by_isrc, outtake], {"s1"}, {"GB1"}) == [outtake]


def test_quarantine_moves_the_file_and_drops_the_row(tmp_path) -> None:
    outtake = _on_disk(tmp_path, item(3, "Outtake"))
    old = Path(outtake.path.decode())
    moved = quarantine([outtake], tmp_path / "replaced")
    assert outtake.removed and not old.exists()
    assert moved == [tmp_path / "replaced" / f"3-{old.name}"] and moved[0].read_bytes() == b"audio"


class FakeLib:
    def __init__(self, items):
        self.items = items

    def items_by_source(self, source):
        return [i for i in self.items if source in (i.get("sources") or "").split(",")]


def test_rollback_release_takes_only_this_imports_unmatched_usenet_items(tmp_path) -> None:
    spotdl_dir = spotdl(tmp_path, "later", [{"song_id": "s1"}])
    since = 1000.0
    matched = _on_disk(tmp_path, item(1, "Once in a Lifetime", added=since + 5, via="usenet",
                                      sources="later", spotify_ids="s1"))
    outtake = _on_disk(tmp_path, item(2, "Outtake", added=since + 5, via="usenet", sources="later"))
    older = _on_disk(tmp_path, item(3, "Bonus", added=since - 5, via="usenet", sources="later"))
    spotdl_item = _on_disk(tmp_path, item(4, "Local", added=since + 5, via="spotdl", sources="later"))
    other_list = _on_disk(tmp_path, item(5, "Other", added=since + 5, via="usenet", sources="keep"))
    lib = FakeLib([matched, outtake, older, spotdl_item, other_list])
    assert rollback_release(lib, "later", since, spotdl_dir, tmp_path / "replaced") == 1
    assert outtake.removed
    assert not any(i.removed for i in (matched, older, spotdl_item, other_list))


def test_cleanup_keeps_a_successful_releases_bonus_tracks() -> None:
    """Sports Team's 7 deluxe extras stay local-only (operator decision on #238)."""
    bonus = item(1, "Bonus", artist="Sports Team", album="Boys These Days (Deluxe Edition)",
                 added=(GRAB.timestamp() + IMPORT.timestamp()) / 2)
    records = {"a": record("imported", name="Boys These Days (Deluxe)", artist="Sports Team")}
    assert failed_release_items([bonus], records) == ([], [bonus], [])


def test_cleanup_takes_items_added_before_the_last_grab() -> None:
    """Talking Heads: the expanded edition failed, the next release succeeded."""
    outtake = item(1, "Outtake", album="Remain in Light (Expanded)", added=GRAB.timestamp() - 600)
    records = {"a": record("imported")}
    assert failed_release_items([outtake], records) == ([outtake], [], [])


def test_cleanup_takes_every_item_of_an_album_that_never_succeeded() -> None:
    """Drake, across its feat. folders: three releases, then fallback."""
    records = {"a": record("fallback", name="Take Care (Deluxe)", artist="Drake", fallback_from="failed")}
    feat = item(1, "HYFR", artist="Drake feat. Lil Wayne", album="Take Care", albumartist="Drake",
                added=IMPORT.timestamp() - 1)
    assert failed_release_items([feat], records) == ([feat], [], [])


def test_cleanup_keeps_a_partial_imports_extras() -> None:
    extra = item(1, "Extra", album="Remain in Light", added=IMPORT.timestamp())
    records = {"a": record("filled", fallback_from="partial")}
    assert failed_release_items([extra], records) == ([], [extra], [])


def test_cleanup_lists_but_keeps_items_no_album_claims() -> None:
    stray = item(1, "Stray", artist="CFCF", album="Something Else", added=IMPORT.timestamp())
    records = {"a": record("wanted", name="L.U.V.", artist="CFCF", attempts=2)}
    assert failed_release_items([stray], records) == ([], [], [stray])


# --- linking instead of quarantining (#240) -----------------------------------


def _drake(playlists) -> dict:
    return {"a": record("fallback", name="Take Care (Deluxe)", artist="Drake", fallback_from="failed",
                        playlists=playlists)}


def test_links_a_feat_credited_album_track_the_library_lacks() -> None:
    proud = item(1, "Make Me Proud", artist="Drake feat. Nicki Minaj", album="Take Care", albumartist="Drake")
    marvin = item(2, "Marvin\u2019s Room", artist="Drake", album="Take Care (Deluxe)")
    extra = item(3, "Headlines", artist="Drake", album="Take Care")
    have = item(4, "Headlines", artist="Drake", album="Take Care", spotify_ids="s3")
    records = _drake({"later": [["Make Me Proud", "Drake", "s1"], ["Marvins Room", "Drake", "s2"],
                                ["Headlines", "Drake", "s3"]]})
    found = links([proud, marvin, extra], records, ItemIndex([have]))
    assert found == [(proud, "s1"), (marvin, "s2")]


def test_links_each_track_once() -> None:
    """The Real Her came twice (Take Care and its Deluxe): one links, the other is a duplicate."""
    first = item(1, "The Real Her", artist="Drake feat. Lil Wayne", album="Take Care")
    second = item(2, "The Real Her", artist="Drake feat. Lil Wayne & André 3000", album="Take Care (Deluxe)")
    records = _drake({"later": [["The Real Her", "Drake", "s1"]], "keep": [["The Real Her", "Drake", "s1"]]})
    assert links([first, second], records, ItemIndex([])) == [(first, "s1")]


def test_links_needs_the_same_title() -> None:
    """Kiss Me and Kiss Me (Euroversion) are different tracks."""
    euro = item(1, "Kiss Me (Euroversion)", artist="CFCF feat. nuum & Seren Forever", album="L.U.V.")
    records = {"a": record("fallback", name="L.U.V.", artist="CFCF", fallback_from="missing",
                           playlists={"later": [["Kiss Me", "CFCF", "s1"], ["Kiss Me - Euroversion", "CFCF", "s2"]]})}
    assert links([euro], records, ItemIndex([])) == [(euro, "s2")]

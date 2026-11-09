"""The Navidrome native-API contract the two-way sync reads (homelab#2361).

``navidrome_responses/*-0.64.2.json`` are shaped from Navidrome v0.64.2's
model JSON tags (``model/playlist.go``: ``Playlist``, ``PlaylistTrack``;
``model/mediafile.go``: ``MediaFile``), not recorded from the live server;
replace them with a recorded response when one is taken.  They pin the fields
the merge uses: a playlist's ``id``, ``path``, ``sync`` and ``ownerName``,
and a track row's ``mediaFileId``, ``libraryPath``, ``path`` and ``missing``.

The trap they pin: ``PlaylistTrack`` embeds ``MediaFile`` and declares its
own ``id``, which shadows the song's, so a row's ``id`` is its position in the
playlist and the song is ``mediaFileId``.  A Navidrome upgrade that breaks
any of this fails here, and the reader is rebuilt (homelab#2361, operator Q1).
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from unittest import mock

from music_scan import navidrome, playlist_shadow, playlist_sync

RESPONSES = Path(__file__).resolve().parent / "navidrome_responses"
URL = "http://navidrome.example.com"
HEADERS = {"X-ND-Authorization": "Bearer jwt"}


def _load(name: str):
    return json.loads((RESPONSES / name).read_text(encoding="utf-8"))


def _resp(body, total=None):
    resp = mock.Mock(status_code=200, headers={} if total is None else {"X-Total-Count": str(total)})
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


def test_playlist_fields():
    for pls in _load("playlists-0.64.2.json"):
        assert isinstance(pls["id"], str) and pls["id"]
        assert isinstance(pls["path"], str)
        assert isinstance(pls["sync"], bool)
        assert isinstance(pls["ownerName"], str)


def test_an_imported_playlist_is_found_by_its_m3u_path():
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp(_load("playlists-0.64.2.json"))):
        found = navidrome.list_playlists(URL, HEADERS)
    by_stem = playlist_shadow._nd_playlists(found, PurePosixPath(navidrome.DEFAULT_PLAYLISTS_DIR))
    assert list(by_stem) == ["aaaaaaah"]
    assert by_stem["aaaaaaah"]["sync"] is True and by_stem["aaaaaaah"]["ownerName"] == "tom"


def test_track_row_fields():
    rows = _load("playlist-tracks-0.64.2.json")
    for row in rows:
        assert isinstance(row["mediaFileId"], str) and row["mediaFileId"]
        assert isinstance(row["libraryPath"], str) and isinstance(row["path"], str)
        assert isinstance(row["missing"], bool)
    # The row's id is its position, not the song's.
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert all(r["id"] != r["mediaFileId"] for r in rows)


def test_track_rows_map_to_merge_tracks():
    rows = _load("playlist-tracks-0.64.2.json")
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp(rows, total=len(rows))):
        read = navidrome.playlist_tracks(URL, HEADERS, "p", page=500)
    music = Path("/root/Music")
    paths = [playlist_shadow.pipeline_path(r, PurePosixPath(navidrome.DEFAULT_MUSIC_DIR), music) for r in read]
    assert paths[0] == Path("/root/Music/library/Artist One/Album One/01 First Song.opus")
    items = {paths[0]: 11, paths[2]: 13}  # the missing track's file left the library
    tracks = [playlist_sync.Track(r["mediaFileId"], items.get(p), bool(r["missing"])) for r, p in zip(read, paths)]
    assert [t.mapped for t in tracks] == [11, None, 13]
    assert tracks[1].missing and tracks[1].song == "1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f"

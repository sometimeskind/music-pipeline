"""Album rows stranded by a playlist that left playlists.conf (homelab#2602): the
album tick requests, looks up, sends back to Usenet or drops each, with an
event, and the fallback never strands one again."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from music_fetch import album_requests as req
from music_fetch import albums, state, stranded

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from mb_fake import OTHER_RG, RG, FakeMB  # noqa: E402

LIVE = "albums"  # the album playlist still in playlists.conf
SP = {name: f"{name}0123456789abcdefABCDEF"[:22] for name in ("pending", "nomatch", "later", "live", "held", "kept")}
RELEASE = "77777777-7777-4777-8777-777777777777"
FOUND_RG = "88888888-8888-4888-8888-888888888888"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")


def _row(name: str, status: str, playlist: str, group: str | None = None, unresolved: str | None = state.PENDING,
         spotify: str | None = None, **extra) -> dict:
    """An album playlist's row on *playlist*, partly fetched: one of its two tracks left to spotdl."""
    if spotify is None and group is None:
        spotify = ("".join(c for c in name if c.isalnum()) + "0123456789abcdefABCDEFGHIJ")[:22]
    tracks = [["One", "Band", f"{name}-1", f"GBAAA{len(name):07d}", 1, 1], ["Two", "Band", f"{name}-2", None, 1, 2]]
    record = {"status": status, "release_group": group, "unresolved": None if group else unresolved,
              "spotify_album_ids": [spotify] if spotify else [], "names": [], "name": name, "artist": "Band",
              "tracks_count": 2, "blocklist": [], "playlists": {playlist: tracks},
              "searched_at": "2026-10-01T00:00:00+00:00", **extra}
    if status == albums.FALLBACK:
        record.update(fallback_from=albums.MISSING, fallback_at="2026-10-02T00:00:00+00:00",
                      fallback_tracks={playlist: [f"{name}-2"]})
    return record


def _setup() -> None:
    """Keep's and Later's leftovers (both playlists left playlists.conf), and the rows the sweep leaves alone."""
    rows = {
        # Keep: never looked up (no items, so canon never did), looked up in vain, keyed, keyed but refused.
        "1": _row("Keep Pending", albums.FALLBACK, "keep", spotify=SP["pending"]),
        "2": _row("Keep No Match", albums.FALLBACK, "keep", unresolved=state.NO_MATCH, spotify=SP["nomatch"]),
        "3": _row("Keep Keyed", albums.FALLBACK, "keep", group=RG),
        "4": _row("Keep Refused", albums.MISSING, "keep", group=OTHER_RG),
        # Later: queued by ref, no release group yet.
        "5": _row("Later Pending", albums.FALLBACK, "later", spotify=SP["later"]),
        # Left alone: a live playlist's, a held one, a requested one, a wants record, one still wanted.
        "6": _row("Live", albums.FALLBACK, LIVE, spotify=SP["live"]),
        "7": _row("Held", albums.IMPORTED, "keep", spotify=SP["held"]),
        "8": _row("Requested", albums.FALLBACK, "keep", spotify=SP["kept"], requested=True),
        "9": _row("Wants", albums.MISSING, "spotdl", origin=albums.WANTS),
        "10": _row("Wanted", albums.WANTED, "keep"),
    }
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=rows))
        conn.execute("INSERT INTO later_queue (ref) VALUES (%s)", (SP["later"],))
        conn.commit()


def _rows() -> albums.State:
    return albums.State.load()


def _by_name(rows: state.AlbumRows) -> dict[str, dict]:
    return {r["name"]: r for r in rows.albums.values()}


def _names(found: list[stranded.Row]) -> list[str]:
    return [r["name"] for _, r in found]


def _events() -> list[tuple[str, str, dict]]:
    with state.connect() as conn:
        return [(k, m, d) for k, m, d in conn.execute(
            "SELECT kind, message, data FROM events WHERE source = %s ORDER BY id", (stranded.SOURCE,))]


def _plan() -> stranded.Plan:
    with state.connect() as conn:
        return stranded.plan(conn, state.load_albums(conn), [LIVE])


class Lookups:
    def __init__(self) -> None:
        self.names: list[list[str]] = []

    def __call__(self, rows: list[stranded.Row]) -> int:
        self.names.append(_names(rows))
        return len(rows)


def _tick(get=None, lookup=None, apply: bool = True) -> None:
    rows = _rows()
    stranded.tick(rows, [LIVE], get, lookup, apply=apply)
    rows.save()


def test_dead_is_an_album_playlists_row_no_configured_playlist_or_request_holds():
    assert stranded.dead({"playlists": {"keep": []}}, [LIVE])
    assert stranded.dead({"playlists": {}}, [LIVE])
    assert not stranded.dead({"playlists": {"keep": [], LIVE: []}}, [LIVE])
    assert not stranded.dead({"playlists": {"keep": []}, "requested": True}, [LIVE])
    assert not stranded.dead({"playlists": {req.SOURCE: []}, "origin": albums.REQUEST}, [LIVE])
    assert not stranded.dead({"playlists": {"spotdl": []}, "origin": albums.WANTS}, [LIVE])


def test_the_plan_sorts_the_stranded_rows():
    _setup()
    p = _plan()
    assert _names(p.later) == ["Later Pending"]
    assert _names(p.request) == ["Keep Keyed", "Keep Refused"]
    assert _names(p.lookup) == ["Keep Pending", "Later Pending"]  # Later's is looked up for later_requests
    assert _names(p.drop) == ["Keep No Match"]


def test_a_dry_run_only_counts(caplog):
    _setup()
    lookups, mb = Lookups(), FakeMB()
    with caplog.at_level(logging.INFO, logger=stranded.__name__):
        _tick(mb, lookups, apply=False)
    assert "[STRANDED] dry run: 1 on Later back to missing, 2 to request, 2 to look up in MusicBrainz, " \
           "1 to drop" in caplog.text
    assert mb.calls == [] and lookups.names == [] and _events() == []
    assert len(_rows().albums) == 10


def test_a_tick_settles_keep_and_later_rows_each_with_an_event(caplog):
    _setup()
    lookups, mb = Lookups(), FakeMB()
    with caplog.at_level(logging.INFO, logger=stranded.__name__):
        _tick(mb, lookups)
    rows = _by_name(_rows())

    # Keep, no release group, looked up in vain: dropped, and its event says why.
    assert "Keep No Match" not in rows
    # Keep with a release group: requested automatically; MusicBrainz refused the other, so it is dropped.
    assert "Keep Keyed" in rows and "Keep Refused" not in rows
    with state.connect() as conn:
        requests = {r.release_group: r for r in req.load(conn)}
    assert requests.keys() == {RG} and requests[RG].requested_by == stranded.BY
    # Later: its Later entry owns it; its dead fallback ends, and Usenet is searched again weekly.
    later = rows["Later Pending"]
    assert later["status"] == albums.MISSING and "fallback_tracks" not in later and "fallback_from" not in later
    # Never looked up: looked up now, Keep's and Later's alike.
    assert lookups.names == [["Keep Pending", "Later Pending"]]
    assert rows["Keep Pending"]["status"] == albums.FALLBACK  # waits for the lookup's answer

    found = {(kind, d["album"] if kind != "album_requested" else ""): (m, d) for kind, m, d in _events()}
    assert set(found) == {("status_changed", "Band — Later Pending"), ("album_dropped", "Band — Keep No Match"),
                          ("album_dropped", "Band — Keep Refused"), ("album_requested", "")}
    message, data = found[("album_dropped", "Band — Keep No Match")]
    assert message.startswith("[STRANDED] ") and "(last: keep)" in message and data["playlists"] == ["keep"]
    assert "MusicBrainz knows no release group" in data["why"] and data["status"] == albums.FALLBACK
    assert "no tracklist for release group" in found[("album_dropped", "Band — Keep Refused")][1]["why"]
    message, data = found[("status_changed", "Band — Later Pending")]
    assert data["from"] == albums.FALLBACK and data["to"] == albums.MISSING and "later" in message
    message, data = found[("album_requested", "")]
    assert data["requested_by"] == stranded.BY and "keep left playlists.conf" in message

    # Left alone.
    assert rows["Live"]["status"] == albums.FALLBACK and rows["Live"]["fallback_tracks"] == {LIVE: ["Live-2"]}
    for name, status in (("Held", albums.IMPORTED), ("Requested", albums.FALLBACK), ("Wants", albums.MISSING),
                         ("Wanted", albums.WANTED)):
        assert rows[name]["status"] == status
    assert "1 on Later back to missing, 1 requested, 2 dropped, 2 looked up" in caplog.text

    # Idempotent: the request is fed (feed_requests marks the row requested); only the lookups remain.
    tick_rows = _rows()
    with state.connect() as conn:
        albums.feed_requests(tick_rows, req.load(conn), [LIVE])
    tick_rows.save()
    calls = len(mb.calls)
    _tick(mb, lookups)
    assert len(mb.calls) == calls and len(_events()) == 4
    assert lookups.names[-1] == ["Keep Pending", "Later Pending"]


def test_a_musicbrainz_outage_stops_the_requests_and_drops_nothing_for_it(caplog):
    _setup()

    def down(path: str, **params):
        raise OSError("connection refused")

    with caplog.at_level(logging.INFO, logger=stranded.__name__):
        _tick(down, Lookups())
    rows = _by_name(_rows())
    assert "Keep Keyed" in rows and "Keep Refused" in rows
    assert "stopped: reading release group" in caplog.text
    assert _names(_plan().request) == ["Keep Keyed", "Keep Refused"]  # the next tick tries again


def _musicbrainz(path: str, **params) -> dict | None:
    """MusicBrainz links one Spotify album (SP["pending"]) to a release; it knows nothing else."""
    if path == "url" and params["resource"].endswith(SP["pending"]):
        return {"relations": [{"target-type": "release", "release": {"id": RELEASE}}]}
    if path == f"release/{RELEASE}":
        return {"id": RELEASE, "release-group": {"id": FOUND_RG}, "artist-credit": []}
    return None


def test_the_lookup_lets_the_resolver_key_a_row_or_mark_it_no_match():
    """The "or one can be resolved" case: a Keep row never looked up is keyed by its Spotify
    album's MusicBrainz link (then requested), and a Later one MusicBrainz doesn't know stays
    Later's, looked up again weekly."""
    _setup()
    _tick(_musicbrainz, stranded.musicbrainz_lookup(_musicbrainz, upc_of=lambda album_id: None))
    with state.connect() as conn:
        cache = state.get_all(conn, state.MB_RELEASES)
    assert cache[SP["pending"]]["fields"]["mb_releasegroupid"] == FOUND_RG
    assert cache[SP["later"]]["rung"] == "none"

    rows = _rows()
    albums.resolve_keys(rows, fixes=[])
    rows.save()
    found = _by_name(_rows())
    assert found["Keep Pending"]["release_group"] == FOUND_RG
    assert found["Later Pending"]["unresolved"] == state.NO_MATCH
    p = _plan()
    assert "Keep Pending" in _names(p.request)
    assert "Later Pending" in _names(p.lookup) and "Later Pending" not in _names(p.drop)  # Later's, not dropped


def test_the_fallback_never_strands_a_row_whose_playlists_left(tmp_path: Path):
    rows = albums.State()
    rows.albums["dead"] = _row("Dead", albums.MISSING, "keep")
    rows.albums["live"] = _row("Live", albums.MISSING, LIVE)

    def missing(playlist: str, tracks: list) -> list:
        return tracks

    albums.fallbacks(rows, missing, album_playlists=[LIVE])
    assert rows.albums["dead"]["status"] == albums.MISSING and "fallback_tracks" not in rows.albums["dead"]
    assert rows.albums["live"]["status"] == albums.FALLBACK


class Spotify:
    def snapshot_id(self, url: str) -> str:
        return "snap"

    def songs(self, url: str) -> list[dict]:
        return []

    def album_songs(self, album_id: str) -> list[dict]:
        return []


def test_the_album_tick_feeds_the_request_the_same_tick_and_keeps_later_off_the_fallback():
    _setup()
    prowlarr, sab = MagicMock(), MagicMock()
    prowlarr.search.return_value = []
    prowlarr.blocked.return_value = []

    def missing(playlist: str, tracks: list) -> list:
        return tracks

    albums.tick([(LIVE, "url")], Spotify(), prowlarr, sab, lambda key, record: False,
                albums.Settings(mode="on", hits_per_day=0, grabs_per_day=0), missing=missing,
                later_get=FakeMB(), stranded_lookup=Lookups())
    rows = _by_name(_rows())
    keyed = next(r for r in rows.values() if r.get("release_group") == RG)
    assert keyed["requested"] and list(keyed["playlists"]) == [req.SOURCE]  # the request's tracklist now
    later = rows["Later Pending"]
    assert later["status"] == albums.MISSING and "fallback_tracks" not in later

"""The album tick reads the library once for all its hooks (homelab#3051). No Prefect server needed.

Each hook used to read the whole library on its first call and keep it until the
tick ended: the ``wants`` and ``mixed`` hooks, ``have`` and the request
``recordings`` each held a copy, and the library feed read a fifth while the
first was held.  On a library of 36,600 items that was 1.6 GB, and the pod's 4Gi
limit went beside a long job."""

from __future__ import annotations

import contextlib
import gc
import tracemalloc
from pathlib import Path
from unittest.mock import MagicMock, patch

TRACKS = [["Song 1", "Artist", "S1", None, 1, 1], ["Song 2", "Artist", "S2", None, 1, 2]]
REQUEST = {"status": "missing", "name": "Album", "artist": "Artist", "tracks_count": 2, "origin": "request",
           "spotify_album_ids": ["ALB0"], "playlists": {"later": TRACKS}}


def _every_hook(calls: list, before=None):
    """A stand-in for ``albums.tick``: calls each hook the tick has, in the tick's
    order, and records the traced memory at its start and once every hook ran."""

    def tick(playlists, spotify, prowlarr, sabnzbd, have, settings, *, on_completion, missing, mixed, wants,
             library, recordings, **_):
        start = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
        wants("mix")
        library()
        mixed("1", {"status": "imported", "spotify_album_ids": ["ALB1"]})
        have("1", REQUEST)
        missing("later", TRACKS)
        recordings("1", REQUEST)
        gc.collect()
        end = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
        calls.append((start, end))
        if before is not None:
            before(on_completion, have)

    return tick


@contextlib.contextmanager
def _tick_over(lib, tick):
    """``flows.album_tick_task`` over *lib* with *tick* for ``albums.tick``, and
    nothing outside the library: no Spotify, indexer, MusicBrainz or stores."""
    import music_fetch.albums as albums
    from music_fetch import album_requests, store, stranded
    from music_scan import want_albums
    from music_service import flows

    playlist = MagicMock(album=False, nosync=False, url="https://open.spotify.com/playlist/P")
    playlist.name = "mix"
    with patch("music_scan.library.MusicLibrary", lambda *_a, **_k: contextlib.nullcontext(lib)), \
         patch.object(flows, "load_playlists", return_value=[playlist]), \
         patch.object(flows, "get_run_logger"), \
         patch.object(flows, "_request_fill", return_value=None), \
         patch("music_fetch.spotdl_ops.SpotifyPlaylists"), \
         patch("music_fetch.usenet.Prowlarr"), \
         patch("music_fetch.usenet.Sabnzbd"), \
         patch.object(album_requests, "musicbrainz"), \
         patch.object(stranded, "musicbrainz_lookup"), \
         patch.object(store, "load", return_value=MagicMock(songs=[{"song_id": "S9", "album_id": "ALB9"}])), \
         patch("music_scan.held.held_urls", return_value=frozenset()), \
         patch.object(want_albums, "read_stores", return_value={}), \
         patch.object(flows.scan, "store_listings", return_value=lambda item: set()), \
         patch.object(albums, "tick", tick):
        yield lambda: flows.album_tick_task.fn(albums.Settings(mode="on"))


def test_every_hook_of_a_tick_shares_one_read_of_the_library():
    lib = MagicMock()
    lib.all_items.return_value = []
    calls: list = []
    with _tick_over(lib, _every_hook(calls)) as run:
        run()
    assert len(calls) == 1
    lib.all_items.assert_called_once_with()


def test_an_import_in_the_tick_drops_the_read_and_the_next_hook_reads_again():
    """The import changed the library, as ``have``'s own read was dropped before."""
    import music_fetch.albums as albums

    lib = MagicMock()
    lib.all_items.return_value = []
    reads: list = []

    def after(complete, have):
        reads.append(lib.all_items.call_count)
        with patch.object(albums, "complete", return_value=None):
            complete(albums.State(), albums.Completion(nzo_id="n", ok=True, path="x"))
        have("1", REQUEST)
        reads.append(lib.all_items.call_count)

    with _tick_over(lib, _every_hook([], after)) as run:
        run()
    assert reads == [1, 2]


def test_the_hooks_hold_one_copy_of_the_library_not_one_each(tmp_path: Path):
    """The largest structure the tick holds is its read of the library.  Before
    homelab#3051 the hooks held four reads once each had run (a fifth, the
    library feed's, was dropped when it returned); now they hold one."""
    from beets.library import Item

    import music_scan.identity  # noqa: F401  # imported by the hooks: not the tick's memory
    import music_scan.music_pipeline  # noqa: F401
    import music_scan.playlists  # noqa: F401
    import music_scan.release  # noqa: F401
    from music_scan.library import MusicLibrary

    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        with lib._lib.transaction():
            for n in range(300):
                item = Item(title=f"Song {n}", artist=f"Artist {n % 40}", album=f"Album {n // 12}", track=n % 12 + 1,
                            isrc=f"QZ{n:010d}", mb_trackid=f"rec-{n}", path=f"/lib/{n}.m4a".encode())
                item["spotify_ids"] = f"sp{n:020d}"
                item["spotify_album_id"] = f"ALB{n // 12}"
                item["album_record"] = str(n // 12)
                item["acoustid_fingerprint"] = "A" * 2800  # about a live item's
                lib._lib.add(item)
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            one = list(lib.all_items())
            gc.collect()
            one_read = tracemalloc.get_traced_memory()[0] - before
            del one
            calls: list = []
            with _tick_over(lib, _every_hook(calls)) as run:
                run()
        finally:
            tracemalloc.stop()
    ((start, end),) = calls
    held = end - start
    # One read and the indexes over it: under 1.5 reads.  Four reads before.
    assert held < 1.5 * one_read, f"the hooks hold {held / one_read:.1f} reads of the library"

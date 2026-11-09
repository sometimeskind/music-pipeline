"""What the scan's ``beet update`` keeps, on a real library with real files (homelab#2723).

beets resets an item's stored ``mtime`` to 0 whenever a tag field (a media field)
changes, so ``beet update`` re-reads the file of every item changed in the
database alone and takes the file's tags back.  A fingerprint stored that way
was gone after the next scan, and the change moved the item, whose suffixed
name then flipped between ``.1`` and ``.2`` because its destination was taken.
"""

import contextlib

import pytest

# A made-up fingerprint: what matters is where it is kept, and CI has no libchromaprint.
FP = "AQAAmadeupfingerprint"


@pytest.fixture
def lib(tmp_path):
    from beets import config
    from beets.library import Library

    config["paths"].set({"default": "$albumartist/$album/$track - $title",
                         "singleton": "$albumartist/$album/$track - $title"})
    library = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "library"))
    yield library
    library._close()


@pytest.fixture
def add(lib, clip, tmp_path):
    """Add a clip to *lib* at its destination, file and database agreeing, as after an import."""
    from beets.library import Item

    count = 0

    def _add(**fields):
        nonlocal count
        count += 1
        path = clip(tmp_path / f"in-{count}.m4a", ("tone", 3))
        item = Item.from_path(str(path))
        item.update({"title": "Made Up Song", "artist": "Made Up Artist", "albumartist": "Made Up Artist",
                     "album": "Made Up Album", "track": 1, **fields})
        lib.add(item)
        item.move()
        item.try_write()
        item.store()
        return item

    return _add


def _update(lib) -> None:
    """The scan's ``beet update``: its fields, moving files as the pipeline's config does."""
    from beets.ui.commands.update import update_items

    from music_scan.process import update_fields

    update_items(lib, list(lib.items()), move=True, pretend=False, fields=update_fields())


def _name(item) -> str:
    return item.path.decode().rsplit("/", 1)[-1]


def test_a_tag_set_in_the_database_alone_is_read_back_from_the_file(lib, add) -> None:
    """Why the items were re-read: the change, not the file, sets the stored mtime to 0."""
    item = add(comments="from the file")
    item["comments"] = "the database only"
    assert item.mtime == 0
    item.store()
    _update(lib)
    assert lib.get_item(item.id).comments == "from the file"


def test_a_flexible_field_does_not_make_beet_update_read_the_file(lib, add) -> None:
    item = add()
    mtime = item.mtime
    item["sources"] = "made-up-playlist"
    item.store()
    assert lib.get_item(item.id).mtime == mtime


def test_a_stored_fingerprint_survives_beet_update(lib, add, monkeypatch) -> None:
    from music_scan import dupes

    monkeypatch.setattr(dupes, "pipeline_lock", contextlib.nullcontext)
    monkeypatch.setattr(dupes, "fingerprint", lambda path: FP)
    item = add()
    assert dupes.fingerprint_missing([lib.get_item(item.id)], apply=True) == 1
    assert lib.get_item(item.id).acoustid_fingerprint == FP
    for _ in range(2):
        _update(lib)
        assert lib.get_item(item.id).acoustid_fingerprint == FP
    assert lib.get_item(item.id).path == item.path


def test_a_refingerprint_and_its_cleared_acoustid_survive_beet_update(lib, add, monkeypatch) -> None:
    from music_scan import dupes

    monkeypatch.setattr(dupes, "pipeline_lock", contextlib.nullcontext)
    monkeypatch.setattr(dupes, "fingerprint", lambda path: FP)
    item = add(acoustid_fingerprint="AQAAoldaudio", acoustid_id="made-up-acoustid")
    assert dupes.fingerprint_missing([lib.get_item(item.id)], apply=True, ids=[item.id]) == 1
    _update(lib)
    after = lib.get_item(item.id)
    assert (after.acoustid_fingerprint, after.acoustid_id) == (FP, "")


def test_the_import_fingerprint_survives_beet_update(lib, add, monkeypatch) -> None:
    """An as-is import's fingerprint (the plugin's item_imported step) is the file's too."""
    from unittest.mock import MagicMock

    from music_scan import music_pipeline

    monkeypatch.setattr(music_pipeline, "fingerprint", lambda path: FP)
    plugin = music_pipeline.MusicPipelinePlugin.__new__(music_pipeline.MusicPipelinePlugin)
    plugin._log = MagicMock()
    item = add()
    plugin.fingerprint_if_missing(lib, lib.get_item(item.id))
    assert lib.get_item(item.id).acoustid_fingerprint == FP
    _update(lib)
    assert lib.get_item(item.id).acoustid_fingerprint == FP


def _twins(add):
    """Two items with one destination: the second sits at the name beets made unique."""
    first, second = add(), add()
    assert (_name(first), _name(second)) == ("01 - Made Up Song.m4a", "01 - Made Up Song.1.m4a")
    return first, second


def _changed_names(lib, item, times: int = 2) -> list[str]:
    """*item*'s name after each of *times* updates that each find a database-only change."""
    names = []
    for n in range(times):
        current = lib.get_item(item.id)
        current["comments"] = f"the database only {n}"
        current.store()
        _update(lib)
        names.append(_name(lib.get_item(item.id)))
    return names


def test_beets_alone_flips_a_suffixed_name_on_every_move(lib, add, monkeypatch) -> None:
    """The flip is beets': unique_path doesn't count the item's own name as unique.

    If this starts failing, beets fixed it and the move_file patch can go."""
    from beets.library import Item

    monkeypatch.setattr(Item, "move_file", getattr(Item.move_file, "__wrapped__", Item.move_file))
    _, second = _twins(add)
    assert _changed_names(lib, second) == ["01 - Made Up Song.2.m4a", "01 - Made Up Song.1.m4a"]


def test_an_item_whose_destination_is_taken_keeps_its_name(lib, add, monkeypatch) -> None:
    from beets.library import Item

    from music_scan import music_pipeline

    monkeypatch.setattr(Item, "move_file", getattr(Item.move_file, "__wrapped__", Item.move_file))
    music_pipeline._patch_move_file()
    music_pipeline._patch_move_file()  # idempotent: the plugin can load twice
    first, second = _twins(add)
    assert _changed_names(lib, second) == ["01 - Made Up Song.1.m4a"] * 2
    assert _name(lib.get_item(first.id)) == "01 - Made Up Song.m4a"


def test_a_suffixed_item_takes_its_destination_once_it_is_free(lib, add, monkeypatch) -> None:
    from beets.library import Item

    from music_scan import music_pipeline

    monkeypatch.setattr(Item, "move_file", getattr(Item.move_file, "__wrapped__", Item.move_file))
    music_pipeline._patch_move_file()
    first, second = _twins(add)
    lib.get_item(first.id).remove(delete=True)
    assert _changed_names(lib, second, times=1) == ["01 - Made Up Song.m4a"]


def test_keeps_suffixed_name(tmp_path) -> None:
    from music_scan.music_pipeline import keeps_suffixed_name

    dest = tmp_path / "01 - Song.m4a"
    one, two, other = (tmp_path / n for n in ("01 - Song.1.m4a", "01 - Song.12.m4a", "01 - Other.1.m4a"))
    for f in (one, two, other):
        f.write_bytes(b"x")
    as_bytes = lambda p: str(p).encode()  # noqa: E731
    assert not keeps_suffixed_name(as_bytes(one), as_bytes(dest))  # the destination is free
    dest.write_bytes(b"x")
    assert keeps_suffixed_name(as_bytes(one), as_bytes(dest))
    assert keeps_suffixed_name(as_bytes(two), as_bytes(dest))
    assert not keeps_suffixed_name(as_bytes(other), as_bytes(dest))
    assert not keeps_suffixed_name(as_bytes(dest), as_bytes(dest))
    assert not keeps_suffixed_name(as_bytes(tmp_path / "01 - Song.1.mp3"), as_bytes(dest))
    assert not keeps_suffixed_name(as_bytes(tmp_path / "elsewhere" / "01 - Song.1.m4a"), as_bytes(dest))


# ISRCs and recording IDs stored without their file (homelab#2726): made-up codes.
ISRC_MB = "XXAAA2600001"
ISRC_SPOTDL = "XXAAA2600002"
SPOTIFY_URL = "https://open.spotify.com/track/abc123"  # conftest's: the clip's own spotdl URL


def _plugin():
    from unittest.mock import MagicMock

    from music_scan import music_pipeline

    plugin = music_pipeline.MusicPipelinePlugin.__new__(music_pipeline.MusicPipelinePlugin)
    plugin._log = MagicMock()
    plugin._pending, plugin._file_tags, plugin._retitled, plugin._beside = {}, {}, {}, {}
    plugin._unwritten = set()
    return plugin


def _survives(lib, item_id, **expected) -> None:
    for _ in range(2):
        _update(lib)
        after = lib.get_item(item_id)
        assert {k: after.get(k) for k in expected} == expected


def test_spotdls_isrc_added_at_import_survives_beet_update(lib, add, monkeypatch) -> None:
    """item_imported runs after beets' import.write: spotdl's ISRC joins MusicBrainz's on the file too."""
    from music_fetch import events
    from music_scan.music_pipeline import Pending, SpotdlTags

    monkeypatch.setattr(events, "record", lambda *a, **kw: None)
    item = lib.get_item(add(isrc=ISRC_MB).id)  # as beets' import left it: MusicBrainz's ISRC, file and database
    plugin = _plugin()
    plugin._pending[id(item)] = Pending(item, "made-up-playlist", "spotdl", None, SpotdlTags(SPOTIFY_URL, ISRC_SPOTDL))
    plugin.tag_source_on_stored(lib, item)
    assert lib.get_item(item.id).isrc == f"{ISRC_MB};{ISRC_SPOTDL}"
    _survives(lib, item.id, isrc=f"{ISRC_MB};{ISRC_SPOTDL}", sources="made-up-playlist")


def test_a_fallbacks_recording_id_set_at_import_survives_beet_update(lib, add, monkeypatch) -> None:
    from music_fetch import events
    from music_fetch.album_requests import recording_url
    from music_scan.music_pipeline import FALLBACK_RECORDING, Pending, SpotdlTags

    monkeypatch.setattr(events, "record", lambda *a, **kw: None)
    recording = "00000000-0000-4000-8000-000000000001"
    item = lib.get_item(add().id)
    plugin = _plugin()
    plugin._pending[id(item)] = Pending(item, "made-up-playlist", "spotdl", None, SpotdlTags(recording_url(recording)))
    plugin.tag_source_on_stored(lib, item)
    assert lib.get_item(item.id).mb_trackid == recording
    _survives(lib, item.id, mb_trackid=recording, **{FALLBACK_RECORDING: recording})


def _listening_plugin(monkeypatch):
    """The plugin as beets loads it, with the listeners it registers, by event."""
    from unittest.mock import MagicMock, patch

    from beets.library import Item
    from beets.plugins import BeetsPlugin

    from music_scan import music_pipeline

    monkeypatch.setattr(Item, "move_file", Item.move_file)  # the plugin patches it at load
    listeners: dict[str, list] = {}
    with patch.object(BeetsPlugin, "register_listener",
                      lambda self, event, fn: listeners.setdefault(event, []).append(fn)):
        with patch.object(BeetsPlugin, "__init__", lambda self, name: None):
            plugin = music_pipeline.MusicPipelinePlugin()
    plugin._log = MagicMock()
    return plugin, listeners


def test_isrcs_merged_into_a_duplicate_survive_beet_update(lib, add, tmp_path, monkeypatch) -> None:
    """[SAME] at import: the library item takes the incoming ISRCs, and its file gets them
    once the import is done (the ``import`` event), not mid-pipeline."""
    from unittest.mock import MagicMock

    from beets import importer as beets_importer
    from beets.library import Item

    from music_fetch import events
    from music_scan import music_pipeline
    from music_scan.music_pipeline import Pending, SpotdlTags

    monkeypatch.setattr(events, "record", lambda *a, **kw: None)
    monkeypatch.setattr(music_pipeline, "SPOTDL_INBOX", tmp_path / "inbox")
    dup = add(isrc=ISRC_MB, via="spotdl", sources="made-up-playlist-a")
    incoming = Item(path=str(tmp_path / "inbox" / "made-up-playlist-b" / "Made Up Song.m4a"),
                    title="Made Up Song", artist="Made Up Artist")
    plugin, listeners = _listening_plugin(monkeypatch)
    tags = SpotdlTags("https://open.spotify.com/track/def456", f"{ISRC_MB};{ISRC_SPOTDL}")
    plugin._pending[id(incoming)] = Pending(incoming, "made-up-playlist-b", "spotdl", None, tags)
    task = MagicMock(item=incoming, items=None, choice_flag=beets_importer.Action.APPLY)
    task.find_duplicates.return_value = []
    task.chosen_info.return_value = {}

    for listener in listeners["import_task_choice"]:
        listener(session=MagicMock(lib=lib), task=task)
    task.set_choice.assert_called_once_with(beets_importer.Action.SKIP)
    assert lib.get_item(dup.id).isrc == f"{ISRC_MB};{ISRC_SPOTDL}"
    for listener in listeners.get("import", []):
        listener(lib=lib, paths=[])
    _survives(lib, dup.id, isrc=f"{ISRC_MB};{ISRC_SPOTDL}", sources="made-up-playlist-a,made-up-playlist-b")


def test_a_backfilled_isrc_survives_beet_update(lib, add, monkeypatch) -> None:
    """music-backfill-ids --apply: the playlist entry's ISRC joins the item's own, on the file too."""
    from unittest.mock import MagicMock, patch

    from music_scan import audit, backfill

    monkeypatch.setattr(audit, "pipeline_lock", contextlib.nullcontext)
    item = add(isrc=ISRC_MB, sources="made-up-playlist", spotify_url=SPOTIFY_URL)
    song = {"name": "Made Up Song", "artists": ["Made Up Artist"], "url": SPOTIFY_URL, "song_id": "abc123",
            "isrc": ISRC_SPOTDL}
    library = MagicMock()
    library.__enter__.return_value = library
    library.all_items.return_value = list(lib.items())
    playlist = MagicMock(url="made-up-playlist")
    playlist.name = "made-up-playlist"
    with patch("music_scan.library.MusicLibrary", return_value=library), \
         patch("music_fetch.config.load_playlists", return_value=[playlist]), \
         patch("music_fetch.spotdl_ops.SpotifyPlaylists") as reader:
        reader.return_value.songs.return_value = [song]
        plan = backfill.run(apply=True)
    assert plan.isrcs_added == 1
    assert lib.get_item(item.id).isrc == f"{ISRC_MB};{ISRC_SPOTDL}"
    _survives(lib, item.id, isrc=f"{ISRC_MB};{ISRC_SPOTDL}", spotify_ids="abc123")


def test_a_backfilled_words_match_stores_words_ids_alone(lib, add, monkeypatch) -> None:
    """homelab#2731: an entry matched by title+artist only records its ID in
    ``words_ids``, a flex field: no ISRC, no ``spotify_ids``, the stored mtime
    kept and the file left as it was."""
    from unittest.mock import MagicMock, patch

    from music_scan import audit, backfill

    monkeypatch.setattr(audit, "pipeline_lock", contextlib.nullcontext)
    item = add(sources="made-up-playlist")
    mtime, file_mtime = item.mtime, item.current_mtime()
    song = {"name": "Made Up Song", "artists": ["Made Up Artist"], "url": "https://open.spotify.com/track/def456",
            "song_id": "def456", "isrc": ISRC_SPOTDL}
    library = MagicMock()
    library.__enter__.return_value = library
    library.all_items.return_value = list(lib.items())
    playlist = MagicMock(url="made-up-playlist")
    playlist.name = "made-up-playlist"
    with patch("music_scan.library.MusicLibrary", return_value=library), \
         patch("music_fetch.config.load_playlists", return_value=[playlist]), \
         patch("music_fetch.spotdl_ops.SpotifyPlaylists") as reader:
        reader.return_value.songs.return_value = [song]
        plan = backfill.run(apply=True)
    assert (plan.by_words, plan.ids_added, plan.isrcs_added, list(plan.changed)) == (1, 0, 0, [item.id])
    stored = lib.get_item(item.id)
    assert (stored.mtime, stored.current_mtime()) == (mtime, file_mtime)
    assert (stored.isrc, stored.get("spotify_ids")) == ("", None)
    _survives(lib, item.id, isrc="", words_ids="def456", spotify_ids=None)


def test_isrc_drift_counts_an_isrc_the_database_alone_holds(lib, add) -> None:
    from music_scan.isrc_drift import drift

    item = add(isrc=ISRC_MB)
    assert drift(lib.items())["isrc differs"] == 0
    stored = lib.get_item(item.id)
    stored["isrc"] = f"{ISRC_MB};{ISRC_SPOTDL}"
    stored.store()
    counts = drift(lib.items())
    assert (counts["isrc differs"], counts["file lacks some of the database's ISRCs"], counts["mtime 0"]) == (1, 1, 1)
    stored.try_write()
    stored.store()
    assert drift(lib.items())["isrc differs"] == 0

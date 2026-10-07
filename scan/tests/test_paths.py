"""Library paths built from tags stay within the filesystem's name limit (#225).

beets caps every path component at ``MAX_FILENAME_LENGTH`` (200) *bytes*, so an
over-long glyph title cannot produce an unwritable library, replace or merge path;
spotdl's inbox names get the same treatment in ``music_fetch.spotdl_ops``.
"""

import pytest

GLYPH = "⃝"  # COMBINING ENCLOSING CIRCLE: one character, three UTF-8 bytes


@pytest.fixture
def lib(tmp_path):
    from beets import config

    from music_scan.library import MusicLibrary

    config["paths"].set({"default": "$albumartist/$album/$track - $title"})
    with MusicLibrary(tmp_path / "library.db", tmp_path / "library") as library:
        yield library


def test_glyph_title_destination_fits_the_name_limit(lib, tmp_path) -> None:
    from beets.library import Item
    from beets.util import MAX_FILENAME_LENGTH

    item = Item(path=b"/inbox/x.m4a", title=GLYPH * 200, artist="Four Tet", albumartist="Four Tet",
                album=GLYPH * 200, track=1, format="AAC")
    lib._lib.add(item)

    dest = item.destination().decode("utf-8")  # a clean decode: cut on a character boundary

    *_, album_dir, name = dest.split("/")
    assert dest.startswith(str(tmp_path / "library"))
    assert len(name.encode("utf-8")) <= MAX_FILENAME_LENGTH
    assert len(album_dir.encode("utf-8")) <= MAX_FILENAME_LENGTH
    assert name.endswith(".m4a")

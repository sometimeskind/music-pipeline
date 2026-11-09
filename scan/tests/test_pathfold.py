"""Library paths as Navidrome tells them apart (homelab#2742)."""

import unicodedata


def test_fold_is_nfc_and_ascii_case_only():
    from music_scan.pathfold import fold

    assert fold("Hot Chip/In Our Heads/04 - Look At.m4a") == fold("hot chip/in our heads/04 - look at.m4a")
    assert fold(unicodedata.normalize("NFD", "Café")) == fold(unicodedata.normalize("NFC", "café"))
    assert fold("É") != fold("é")  # SQLite's NOCASE folds ASCII letters only
    assert fold(b"/music/A.m4a") == fold("/music/a.m4a")


def test_library_paths_and_held_by(tmp_path):
    from beets.library import Item, Library

    from music_scan.pathfold import held_by, library_paths

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "library"))
    try:
        one = lib.add(Item(path=str(tmp_path / "library" / "A" / "01 - X.m4a")))
        two = lib.add(Item(path=str(tmp_path / "library" / "a" / "01 - x.m4a")))
        outside = lib.add(Item(path=str(tmp_path / "elsewhere" / "01 - X.m4a")))
        paths = library_paths(lib)
        assert held_by(paths, tmp_path / "library" / "A" / "01 - X.M4A") == {
            one: str(tmp_path / "library" / "A" / "01 - X.m4a"), two: str(tmp_path / "library" / "a" / "01 - x.m4a")}
        assert set(held_by(paths, tmp_path / "library" / "a" / "01 - X.m4a", own=one)) == {two}
        assert set(held_by(paths, tmp_path / "elsewhere" / "01 - x.m4a")) == {outside}
    finally:
        lib._close()

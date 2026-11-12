"""MusicLibrary's beets transaction after a failed COMMIT (#506)."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path

import pytest


@contextlib.contextmanager
def _reader(db: Path):
    """Another connection holding SQLite's SHARED lock, so a COMMIT can't get EXCLUSIVE."""
    conn = sqlite3.connect(str(db), isolation_level=None, timeout=0.2)
    conn.execute("BEGIN")
    conn.execute("SELECT * FROM items").fetchall()
    try:
        yield
    finally:
        conn.execute("COMMIT")
        conn.close()


def _short_timeout(lib) -> None:
    """A 0.2 s busy timeout instead of beets' 5 s, for the connections opened from here."""
    lib._lib.timeout = 0.2
    lib._lib._close()


def _store_in_thread(item) -> bool:
    """Whether *item*.store() returned within 5 s: before the fix it blocked forever."""
    done: list = []
    thread = threading.Thread(target=lambda: done.append(item.store()), daemon=True)
    thread.start()
    thread.join(5)
    return bool(done)


def test_a_failed_commit_rolls_back_and_frees_the_library(tmp_path: Path, caplog) -> None:
    """beets commits before it releases its lock, not in a finally: a COMMIT that
    timed out on a reader left the transaction open (every other connection then
    got 'database is locked') and the next store on the library hung for good.
    That hung album import tomato-gaur for 4 h."""
    from beets.library import Item

    from music_scan.library import MusicLibrary

    db = tmp_path / "library.db"
    with MusicLibrary(db, tmp_path) as lib:
        _short_timeout(lib)
        shadow = Item(title="Shadow", path=b"/lib/shadow.m4a")
        switch = Item(title="Switch", path=b"/lib/switch.m4a")
        lib._lib.add(shadow)
        lib._lib.add(switch)

        with _reader(db):
            shadow.title = "Shadow (retag)"
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                shadow.store()
            assert not lib._lib._db_lock.locked()
            assert not lib._lib._connection().in_transaction
        assert "[DBLOCK]" in caplog.text

        # Another connection reads, and the next store on this library returns.
        with contextlib.closing(sqlite3.connect(str(db), timeout=0.2)) as other:
            assert other.execute("SELECT title FROM items ORDER BY id").fetchall() == [("Shadow",), ("Switch",)]
        switch.title = "Switch (retag)"
        assert _store_in_thread(switch)
        assert [i.title for i in lib.all_items()] == ["Shadow", "Switch (retag)"]


def test_a_nested_transaction_still_commits_only_at_the_root(tmp_path: Path) -> None:
    from beets.library import Item

    from music_scan.library import MusicLibrary

    db = tmp_path / "library.db"
    with MusicLibrary(db, tmp_path) as lib:
        item = Item(title="One", path=b"/lib/one.m4a")
        lib._lib.add(item)
        with lib._lib.transaction():
            item.title = "Two"
            item.store()  # nested: no commit yet
            assert lib._lib._connection().in_transaction
        assert not lib._lib._db_lock.locked()
        assert [i.title for i in lib.all_items()] == ["Two"]

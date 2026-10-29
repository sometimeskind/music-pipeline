"""The one-shot JSON import (homelab#2410)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from music_fetch import state, state_import

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"


@pytest.fixture
def inbox(tmp_path) -> Path:
    spotdl = tmp_path / "inbox" / "spotdl"
    (spotdl / ".store").mkdir(parents=True)
    (spotdl / ".albums.json").write_text(json.dumps({
        "playlists": {"later": {"snapshot_id": "snap"}},
        "albums": {
            "sp1": {"status": "imported", "name": "A", "artist": "X", "blocklist": []},
            "sp2": {"status": "wanted", "name": "A (Deluxe)", "artist": "X", "blocklist": ["r9"]},
            "sp3": {"status": "missing", "name": "B", "artist": "Y", "blocklist": []},
            "local/files": {"status": "have", "name": "files", "artist": "local", "blocklist": []},
        },
        "indexer": [{"t": "2026-10-29T10:00:00+00:00", "kind": "grab"}],
        "mixed": ["sp1"],
    }))
    (spotdl / ".mb-releases.json").write_text(json.dumps({
        "sp1": {"rung": "url", "fields": {"mb_releasegroupid": RG}},
        "sp2": {"rung": "isrc", "fields": {"mb_releasegroupid": RG}},
        "sp3": {"rung": "none", "checked": "2026-10-01T00:00:00+00:00"},
    }))
    (spotdl / ".spotify-rate-limit.json").write_text(json.dumps({"until": "2026-10-29T00:00:00+00:00",
                                                                  "retry_after": 60}))
    (spotdl / ".store" / "later.json").write_text(json.dumps({
        "version": 1, "playlist": "later", "updated": "2026-10-29T09:00:00Z", "source": "spotify",
        "entries": [{"song": {"url": "u1", "album_id": "sp1"}}]}))
    (tmp_path / "inbox" / ".spotdl-failures.json").write_text(json.dumps({"u9": {"attempts": 2}}))
    return spotdl


def test_plan_rekeys_merges_and_flags(inbox, tmp_path):
    p = state_import.plan(inbox, tmp_path / "no-library.db")
    assert list(p.rows.albums) == ["sp1", "sp3", "local/files"]
    assert p.rows.albums["sp1"]["release_group"] == RG
    assert p.rows.albums["sp1"]["spotify_album_ids"] == ["sp1", "sp2"]
    assert p.rows.albums["sp1"]["blocklist"] == ["r9"]
    assert p.rows.albums["sp3"]["unresolved"] == state.NO_MATCH
    assert p.rows.albums["local/files"]["names"] == ["local/files"]
    assert p.rows.mixed == ["sp1"]
    assert p.report.summary() == "1 rekeyed, 2 flagged (1 no-match, 1 pending), 1 merged into 1 album(s), 0 conflict(s)"
    assert set(p.kv) == {state.MB_RELEASES, state.SPOTIFY_LIMIT}
    assert list(p.stores) == ["later"] and p.failures == {"u9": {"attempts": 2}}


@needs_pg
def test_write_imports_everything_once(inbox, tmp_path, pg_url):
    p = state_import.plan(inbox, tmp_path / "no-library.db")
    with state.connect() as conn:
        state_import.write(conn, p)
        rows = state.load_albums(conn)
        assert list(rows.albums) == ["sp1", "sp3", "local/files"]
        assert rows.by_ref()["sp2"] == "sp1"
        assert rows.mixed == ["sp1"]
        assert rows.indexer == [{"t": "2026-10-29T10:00:00+00:00", "kind": "grab"}]
        assert state.load_store(conn, "later")["updated"] == "2026-10-29T09:00:00Z"
        assert state.load_failures(conn) == {"u9": {"attempts": 2}}
        assert state.get(conn, state.SPOTIFY_LIMIT)["retry_after"] == 60
        assert state.get_all(conn, state.MB_RELEASES)["sp3"]["rung"] == "none"
        assert "1 rekeyed" in state.get(conn, state.IMPORTED)["summary"]
        with pytest.raises(SystemExit, match="--replace"):
            state_import.write(conn, state_import.plan(inbox, tmp_path / "no-library.db"))
        state_import.write(conn, state_import.plan(inbox, tmp_path / "no-library.db"), replace=True)
        assert len(state.load_albums(conn).albums) == 3
        assert conn.execute("SELECT min(id) FROM albums").fetchone() == (1,)

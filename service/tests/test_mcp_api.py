"""Tests for the /mcp-api/* routes (homelab#2412), against a real beets library."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from music_fetch import albums, events, overrides, overrides_cli, state
from music_service import mcp_jobs, mcp_reads

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
import mcp_harness  # noqa: E402

MCP = {"Authorization": "Bearer mcp-secret", "X-MCP-Caller": "sub-1"}
WRITE = {**MCP, "X-MCP-Tier": "write"}
READ = {**MCP, "X-MCP-Tier": "read"}

@pytest.fixture
def env(tmp_path, monkeypatch):
    seeded = mcp_harness.seed(tmp_path)
    attrs, environ = mcp_harness.patches(seeded)
    for module, name, value in attrs:
        monkeypatch.setattr(module, name, value)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("API_BEARER_TOKEN", "api-secret")
    monkeypatch.setenv("MCP_API_TOKEN", "mcp-secret")
    monkeypatch.setenv("MCP_INLINE_SECONDS", "5")
    commands = mcp_harness.Commands()
    app, jobs = mcp_harness.make_app(seeded, commands)
    return {"client": app.test_client(), "commands": commands, "jobs": jobs, "db": seeded["db"],
            "items": seeded["items"]}


@pytest.fixture
def client(env):
    return env["client"]


# ---------------------------------------------------------------------------
# Auth and headers
# ---------------------------------------------------------------------------


def test_only_the_mcp_token_opens_mcp_api(client):
    assert client.get("/mcp-api/library_status", headers=MCP).status_code == 200
    assert client.get("/mcp-api/library_status", headers={**MCP, "Authorization": "Bearer api-secret"}).status_code == 401
    assert client.get("/mcp-api/library_status", headers={"X-MCP-Caller": "x"}).status_code == 401
    # The MCP token opens nothing else.
    assert client.get("/inbox", headers={"Authorization": "Bearer mcp-secret"}).status_code == 401


def test_mcp_api_is_closed_without_its_token(client, monkeypatch):
    monkeypatch.delenv("MCP_API_TOKEN")
    assert client.get("/mcp-api/library_status", headers={**MCP, "Authorization": "Bearer "}).status_code == 401


def test_caller_header_is_required(client):
    resp = client.get("/mcp-api/library_status", headers={"Authorization": "Bearer mcp-secret"})
    assert resp.status_code == 400 and "X-MCP-Caller" in resp.json["error"]
    assert client.get("/mcp-api/library_status", headers={**MCP, "X-MCP-Tier": "admin"}).status_code == 400


@pytest.mark.parametrize("headers", [MCP, READ])
def test_write_routes_need_the_write_tier(client, env, headers):
    for path in ["/mcp-api/run/embed_covers", "/mcp-api/run/reset_album", "/mcp-api/trigger/scan",
                 "/mcp-api/trigger/album_tick"]:
        resp = client.post(path, json={}, headers=headers)
        assert resp.status_code == 403, path
    assert env["commands"].ran == []


def test_read_tier_runs_the_audit_dry_runs(client, env):
    resp = client.post("/mcp-api/run/audit_lengths", json={"silence": True}, headers=READ)
    assert resp.status_code == 200 and resp.json["status"] == "done"
    assert env["commands"].ran == [["music-audit-lengths", "--silence"]]
    assert resp.json["caller"] == "sub-1" and resp.json["tier"] == "read"
    assert client.post("/mcp-api/run/audit_lengths", json={"apply": True}, headers=WRITE).status_code == 400
    titles = client.post("/mcp-api/run/audit_titles", json={}, headers=READ)
    assert titles.status_code == 200 and env["commands"].ran[-1] == ["music-audit-titles"]
    assert client.post("/mcp-api/run/audit_titles", json={"album": "x"}, headers=READ).status_code == 400
    artists = client.post("/mcp-api/run/audit_artists", json={}, headers=READ)
    assert artists.status_code == 200 and env["commands"].ran[-1] == ["music-audit-artists"]
    grabs = client.post("/mcp-api/run/audit_grabs", json={}, headers=READ)
    assert grabs.status_code == 200 and env["commands"].ran[-1] == ["music-audit-grabs"]


def test_read_tier_checks_a_release_against_the_matcher(client, env):
    body = {"album": "pastel", "title": "  Nina Simone-Pastel   Blues-FLAC ", "size_bytes": 400000000,
            "words": "Pastel Blues -Live"}
    resp = client.post("/mcp-api/run/check_release", json=body, headers=READ)
    assert resp.status_code == 200 and resp.json["status"] == "done"
    assert env["commands"].ran[-1] == ["music-check-release", f"--album={resp.json['args']['album']}",
                                       "--title=Nina Simone-Pastel Blues-FLAC", "--size=400000000",
                                       "--words=pastel blues live"]
    bare = client.post("/mcp-api/run/check_release", json={"album": "pastel", "title": "x"}, headers=READ)
    assert bare.status_code == 200 and len(env["commands"].ran[-1]) == 3


def test_check_release_refuses_a_size_or_words_it_cant_use(client):
    for extra in [{"size_bytes": 0}, {"size_bytes": "big"}, {"size_bytes": True}, {"words": "♫"}, {"apply": True}]:
        body = {"album": "pastel", "title": "x", **extra}
        assert client.post("/mcp-api/run/check_release", json=body, headers=READ).status_code == 400, extra
    assert client.post("/mcp-api/run/check_release", json={"album": "pastel"}, headers=READ).status_code == 400


# ---------------------------------------------------------------------------
# Structured reads
# ---------------------------------------------------------------------------


def test_library_status(client):
    body = client.get("/mcp-api/library_status", headers=MCP).json
    assert body["items"] == 3
    assert body["albums"] == {"total": 3, "by_status": {"imported": 1, "wanted": 1, "failed": 1}}
    assert body["quarantine"] == {"unmatched": 1, "rejected": 1, "replaced": 2, "keep": 0}
    assert body["playlists"] == ["chill"]
    assert body["jobs"] == {"running": None, "queued": 0, "depth": 5, "long": None}


def test_find_albums_lists_every_match_with_item_counts(client):
    body = client.get("/mcp-api/albums?query=abbey", headers=MCP).json
    assert [(a["key"], a["items"]) for a in body["albums"]] == [("ALB1", 2), ("ALB2", 0)]


def test_get_album_resolves_one_album_or_returns_candidates(client, env):
    body = client.get("/mcp-api/album?query=the beatles — abbey road", headers=MCP).json
    assert body["status"] == "ok" and body["album"]["key"] == body["album"]["spotify_id"] == "ALB1"
    assert body["beets"]["item_ids"] == [env["items"][0].id, env["items"][1].id]
    assert body["items"][0]["path"] == "The Beatles/Abbey Road/01 - Come Together.m4a"
    amb = client.get("/mcp-api/album?query=abbey", headers=MCP)
    assert amb.status_code == 200 and amb.json["status"] == "ambiguous"
    assert [c["key"] for c in amb.json["candidates"]] == ["ALB1", "ALB2"]
    assert client.get("/mcp-api/album?query=nothing", headers=MCP).status_code == 404


def test_get_album_says_what_the_blocklist_holds_and_keeps_the_stored_list(client, env):
    _blocklisted_album()
    album = client.get("/mcp-api/album?query=pastel", headers=MCP).json["album"]
    assert album["record"]["blocklist"] == ["https://indexer/details/7"]
    (entry,) = album["blocklisted"]
    assert entry["guid"] == "https://indexer/details/7" and entry["title"] == "Nina.Simone-Pastel.Blues-FLAC-GRP"
    assert entry["size"] == 300_000_000 and entry["reason"] == "Aborted, encryption detected" and entry["at"]


def test_get_album_answers_with_the_reasons_unknown_when_the_events_cant_be_read(client, env):
    _blocklisted_album()
    with patch("music_fetch.blocklist.album_events", side_effect=psycopg.OperationalError("down")):
        album = client.get("/mcp-api/album?query=pastel", headers=MCP).json["album"]
    assert album["blocklisted"][0]["reason"] == "unknown: the event store can't be read"


def test_get_item(client, env):
    item = env["items"][0]
    body = client.get(f"/mcp-api/items/{item.id}", headers=MCP).json
    assert body["item"]["title"] == "Come Together"
    assert body["item"]["path"] == "The Beatles/Abbey Road/01 - Come Together.m4a"
    assert body["item"]["fields"]["spotify_ids"] == "S1"
    assert body["item"]["added"].endswith("Z")
    assert body["listed_on"] == ["chill"]
    assert client.get("/mcp-api/items/999", headers=MCP).status_code == 404


def test_get_playlist_resolves_entries_against_the_library(client, env):
    body = client.get("/mcp-api/playlists/chill", headers=MCP).json
    assert body["counts"] == {"entries": 4, "resolved": 3, "missing": 1}
    ids = [e["item_id"] for e in body["entries"]]
    assert ids == [env["items"][0].id, env["items"][2].id, None, env["items"][1].id]  # by ISRC for the 2nd
    assert body["entries"][3]["kind"] == "local" and body["next_offset"] is None
    assert client.get("/mcp-api/playlists/nope", headers=MCP).status_code == 404
    assert client.get("/mcp-api/playlists/..%2F.albums", headers=MCP).status_code == 404


def test_list_quarantine(client):
    body = client.get("/mcp-api/quarantine?kind=replaced&query=FOUR", headers=MCP).json
    assert [f["path"] for f in body["files"]] == ["replaced/4-four.m4a"]
    body = client.get("/mcp-api/quarantine?kind=unmatched", headers=MCP).json
    assert [f["path"] for f in body["files"]] == ["Artist/one.m4a"]
    assert client.get("/mcp-api/quarantine?kind=all", headers=MCP).status_code == 400


def test_list_quarantine_keep(env, tmp_path):
    """keep/ (homelab#2623) is its own kind, never unmatched."""
    kept = tmp_path / "quarantine" / "keep" / "17052-live.m4a"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x")
    client = env["client"]
    body = client.get("/mcp-api/quarantine?kind=keep", headers=MCP).json
    assert [f["path"] for f in body["files"]] == ["keep/17052-live.m4a"]
    body = client.get("/mcp-api/quarantine?kind=unmatched", headers=MCP).json
    assert [f["path"] for f in body["files"]] == ["Artist/one.m4a"]
    assert client.get("/mcp-api/library_status", headers=MCP).json["quarantine"]["keep"] == 1


@pytest.mark.parametrize("file, expected", [
    ("17052", "17052"),
    (" replaced/17052-03-10 - Live.m4a ", "17052-03-10 - Live.m4a"),
    ("--apply", "--apply"),  # passed as --keep=--apply: never a flag
])
def test_keep_quarantined_normalises_the_file(client, env, file, expected):
    resp = client.post("/mcp-api/run/keep_quarantined", json={"file": file}, headers=WRITE)
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"file": expected}
    assert env["commands"].ran[-1] == ["music-album-audit-releases", f"--keep={expected}"]


@pytest.mark.parametrize("file", ["", "../x.m4a", "/etc/passwd", "a/../../b", 7, None, "x" * 501])
def test_keep_quarantined_refuses_a_path_outside_replaced(client, file):
    assert client.post("/mcp-api/run/keep_quarantined", json={"file": file}, headers=WRITE).status_code == 400


def test_retire_item_normalises_its_ids_and_reason(client, env):
    resp = client.post("/mcp-api/run/retire_item", json={"ids": [8283, 534, 8283], "reason": "  wrong\n box  set "},
                       headers=WRITE)
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"ids": [534, 8283], "reason": "wrong box set"}
    assert env["commands"].ran[-1] == ["music-album-audit-releases", "--reason=wrong box set", "--retire", "534",
                                       "8283"]


def test_retire_item_keeps_a_flag_like_reason_an_option_value(client, env):
    resp = client.post("/mcp-api/run/retire_item", json={"ids": [8283], "reason": "--apply"}, headers=WRITE)
    assert resp.status_code == 200, resp.json
    assert env["commands"].ran[-1] == ["music-album-audit-releases", "--reason=--apply", "--retire", "8283"]


@pytest.mark.parametrize("body", [
    {"reason": "why"}, {"ids": [], "reason": "why"}, {"ids": [0], "reason": "why"}, {"ids": ["8283"], "reason": "why"},
    {"ids": 8283, "reason": "why"}, {"ids": [8283]}, {"ids": [8283], "reason": "  "}, {"ids": [8283], "reason": 7},
    {"ids": [8283], "reason": "x" * 501}, {"ids": [8283], "reason": "why", "force": True},
])
def test_retire_item_refuses_bad_arguments(client, body):
    assert client.post("/mcp-api/run/retire_item", json=body, headers=WRITE).status_code == 400, body


def test_a_locked_library_returns_busy_retry(client, env, monkeypatch):
    monkeypatch.setattr(mcp_reads, "BUSY_TIMEOUT_SECONDS", 0.1)
    locker = sqlite3.connect(env["db"])
    locker.execute("BEGIN EXCLUSIVE")
    try:
        resp = client.get(f"/mcp-api/items/{env['items'][0].id}", headers=MCP)
    finally:
        locker.rollback()
        locker.close()
    assert resp.status_code == 503 and resp.json == {"status": "busy", "retry": True,
                                                     "error": "beets library is locked"}


def test_reads_never_write_the_library(client, env):
    before = env["db"].read_bytes()
    client.get("/mcp-api/library_status", headers=MCP)
    client.get(f"/mcp-api/items/{env['items'][0].id}", headers=MCP)
    assert env["db"].read_bytes() == before


# ---------------------------------------------------------------------------
# Write tools and the dry-run → apply guard
# ---------------------------------------------------------------------------


def test_reset_album_dry_run_then_apply(client, env):
    env["commands"].outputs["music-album-reset -- ALB3"] = (["[2026-10-27T10:00:00+0000] ALB3: status failed → wanted"], 0)
    dry = client.post("/mcp-api/run/reset_album", json={"query": "pastel"}, headers=WRITE)
    assert dry.status_code == 200 and dry.json["status"] == "done" and dry.json["mode"] == "dry_run"
    assert dry.json["args"] == {"album": "ALB3", "reimport": False}
    # Another caller, another spelling of the same album.
    other = {**WRITE, "X-MCP-Caller": "sub-2"}
    env["commands"].outputs["music-album-reset -- ALB3"] = (["[2026-10-27T10:05:00+0000] ALB3: status failed → wanted"], 0)
    applied = client.post("/mcp-api/run/reset_album", json={"query": "Nina Simone — Pastel Blues", "apply": True},
                          headers=other)
    assert applied.status_code == 200 and applied.json["status"] == "done", applied.json
    assert env["commands"].ran[-1] == ["music-album-reset", "--apply", "--", "ALB3"]
    assert applied.json["locks"] == ["albums", "pipeline"]


class RealCli(mcp_harness.Commands):
    """The real music-album-reset and music-overrides, in this process: each run's
    lines carry a log timestamp of their own, as a subprocess's do."""

    def __init__(self) -> None:
        super().__init__()
        self.runs = 0

    def __call__(self, argv, env, on_line):
        self.ran.append(argv)
        self.runs += 1
        stamp = f"[2026-11-05T10:{self.runs:02d}:00+0000] "

        class Lines(logging.Handler):
            def emit(self, record):
                on_line(stamp + record.getMessage())

        handler = Lines(logging.INFO)
        loggers = [logging.getLogger("music_fetch.albums"), logging.getLogger("music_fetch.overrides_cli")]
        levels = [log.level for log in loggers]
        for log in loggers:
            log.addHandler(handler)
            log.setLevel(logging.INFO)
        try:
            if argv[0] == "music-album-reset":
                with patch("sys.argv", argv):
                    albums.reset_main()
            else:
                overrides_cli.main(argv[1:])
        finally:
            for log, level in zip(loggers, levels):
                log.removeHandler(handler)
                log.setLevel(level)
        return 0


@pytest.fixture
def real_cli(env, tmp_path, monkeypatch):
    cli = RealCli()
    monkeypatch.setattr(env["jobs"], "_run_command", cli)
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")
    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    return cli


def _blocklisted_album() -> None:
    """Pastel Blues with a broken download on its blocklist, and the events that say so."""
    rows = albums.State.load()
    rows.albums["ALB3"].update(blocklist=["https://indexer/details/7"], attempts=1, duration=2400, tracks_count=9,
                               candidate={"guid": "https://indexer/details/7", "title": "Nina.Simone-Pastel.Blues-FLAC-GRP",
                                          "size": 300_000_000})
    rows.save()
    events.record("grab", "[GRAB]", source="albums", data={
        "key": "ALB3", "guid": "https://indexer/details/7", "release": "Nina.Simone-Pastel.Blues-FLAC-GRP",
        "size": 300_000_000})
    events.record("failure", "[FAIL]", source="albums", data={"key": "ALB3", "reason": "Aborted, encryption detected"})


def test_the_reset_dry_run_passes_the_apply_gate(env, real_cli):
    """The lines reset_notes adds repeat exactly, so the apply's fresh dry run matches, and the apply lands."""
    _blocklisted_album()
    client = env["client"]
    dry = client.post("/mcp-api/run/reset_album", json={"query": "pastel", "reimport": True}, headers=WRITE)
    assert dry.json["status"] == "done", dry.json
    output = dry.json["output"]
    assert "Nina.Simone-Pastel.Blues-FLAC-GRP (300 MB): Aborted, encryption detected; the matcher accepts it again" in output
    assert "up to 1 of the 3 the album then has" in output
    applied = client.post("/mcp-api/run/reset_album", json={"query": "pastel", "reimport": True, "apply": True},
                          headers=WRITE)
    assert applied.json["status"] == "done", applied.json
    assert real_cli.ran[-1][:2] == ["music-album-reset", "--reimport"] and "--apply" in real_cli.ran[-1]
    record = albums.State.load().albums["ALB3"]
    assert record["blocklist"] == [] and record["attempts"] == 0 and record["reimport"] is True


def test_an_event_that_changes_the_reason_refuses_the_reset(env, real_cli):
    """The compared output does hold the reasons: one that changed is a dry run to show again."""
    _blocklisted_album()
    client = env["client"]
    client.post("/mcp-api/run/reset_album", json={"query": "pastel"}, headers=WRITE)
    events.record("grab", "[GRAB]", source="albums", data={
        "key": "ALB3", "guid": "https://indexer/details/7", "release": "Nina.Simone-Pastel.Blues-FLAC-GRP",
        "size": 300_000_000})
    events.record("failure", "[FAIL]", source="albums", data={"key": "ALB3", "reason": "Repair failed"})
    applied = client.post("/mcp-api/run/reset_album", json={"query": "pastel", "apply": True}, headers=WRITE)
    assert applied.json["status"] == "refused", applied.json


def test_the_override_dry_run_passes_the_apply_gate(env, real_cli):
    with state.connect() as conn:
        state.put(conn, state.OVERRIDES_IMPORTED, {"at": "now"})
    client = env["client"]
    body = {"album": "pastel", "words": "Pastel Blues"}
    dry = client.post("/mcp-api/run/set_search_override", json=body, headers=WRITE)
    assert dry.json["status"] == "done", dry.json
    output = dry.json["output"]
    assert "Every word must be in a release title" in output and "nina isn't among these words" in output
    applied = client.post("/mcp-api/run/set_search_override", json={**body, "apply": True}, headers=WRITE)
    assert applied.json["status"] == "done", applied.json
    search, _ = overrides.keyed(albums.album_fixes(), albums.State.load())
    assert search == {"ALB3": "pastel blues"}


def test_a_dashed_override_word_is_a_400(client, env):
    resp = client.post("/mcp-api/run/set_search_override", json={"album": "pastel", "words": "Pastel Blues -Live"},
                       headers=WRITE)
    assert resp.status_code == 400 and "no exclusion syntax" in resp.json["error"]
    assert env["commands"].ran == []


def test_check_release_still_shows_what_a_dashed_word_does(client, env):
    resp = client.post("/mcp-api/run/check_release", headers=READ, json={
        "album": "pastel", "title": "Nina Simone-Pastel Blues-FLAC", "words": "Pastel Blues -Live"})
    assert resp.status_code == 200 and resp.json["args"]["words"] == "pastel blues live"


def test_apply_without_a_dry_run_is_refused(client, env):
    resp = client.post("/mcp-api/run/embed_covers", json={"apply": True}, headers=WRITE)
    assert resp.status_code == 409 and resp.json["reason"] == "no_dry_run"
    assert env["commands"].ran == []
    # A dry run with other args doesn't count.
    client.post("/mcp-api/run/canon_albums", json={"refresh": True}, headers=WRITE)
    assert client.post("/mcp-api/run/canon_albums", json={"apply": True}, headers=WRITE).status_code == 409


def test_apply_refuses_and_returns_the_new_output_when_it_changed(client, env):
    env["commands"].outputs["music-embed-covers"] = (["3 item(s) would get a cover"], 0)
    client.post("/mcp-api/run/embed_covers", json={}, headers=WRITE)
    env["commands"].outputs["music-embed-covers"] = (["4 item(s) would get a cover"], 0)
    resp = client.post("/mcp-api/run/embed_covers", json={"apply": True}, headers=WRITE)
    assert resp.json["status"] == "refused" and resp.json["output"].startswith("4 item(s)")
    assert ["music-embed-covers", "--apply"] not in env["commands"].ran
    # The refused run is the dry run now: applying again goes through.
    resp = client.post("/mcp-api/run/embed_covers", json={"apply": True}, headers=WRITE)
    assert resp.json["status"] == "done" and env["commands"].ran[-1] == ["music-embed-covers", "--apply"]


def test_ambiguous_reset_does_nothing(client, env):
    resp = client.post("/mcp-api/run/reset_album", json={"query": "abbey"}, headers=WRITE)
    assert resp.status_code == 200 and resp.json["status"] == "ambiguous"
    assert len(resp.json["candidates"]) == 2
    assert env["commands"].ran == [] and env["jobs"].jobs == {}


def test_merge_items_runs_one_command_per_pair(client, env):
    resp = client.post("/mcp-api/run/merge_items", json={"pairs": [[1, 2], [3, 4]]}, headers=WRITE)
    assert resp.json["argv"] == [["music-audit-dupes", "--merge", "1", "2"], ["music-audit-dupes", "--merge", "3", "4"]]
    assert client.post("/mcp-api/run/merge_items", json={"pairs": [[1, 2], [2, 3]]}, headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/merge_items", json={"pairs": [[1, 1]]}, headers=WRITE).status_code == 400


def test_requeue_normalises_since_to_utc(client, env):
    resp = client.post("/mcp-api/run/requeue_albums", json={"since": "2026-10-20T02:00:00+02:00", "ids": ["B", "A", "A"]},
                       headers=WRITE)
    assert resp.json["args"] == {"ids": ["A", "B"], "since": "2026-10-20T00:00:00+00:00"}
    assert resp.json["argv"] == [["music-album-requeue", "--since", "2026-10-20T00:00:00+00:00", "--", "A", "B"]]
    assert client.post("/mcp-api/run/requeue_albums", json={}, headers=WRITE).status_code == 400


RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"
RG2 = "0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10"
REL = "11111111-1111-4111-8111-111111111111"


def test_request_album_normalises_the_release_groups(client, env):
    """homelab#2478: MBIDs or musicbrainz.org URLs, deduplicated and sorted, each with its preferred release."""
    resp = client.post("/mcp-api/run/request_album", headers=WRITE, json={"albums": [
        {"release_group": RG}, {"release_group": f"https://musicbrainz.org/release-group/{RG2.upper()}",
                                "preferred_release": f"https://musicbrainz.org/release/{REL}"},
        {"release_group": RG, "preferred_release": ""}]})
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"albums": [{"release_group": RG, "preferred_release": None},
                                            {"release_group": RG2, "preferred_release": REL}], "later": False}
    assert env["commands"].ran[-1] == ["music-requests", "add", "--", RG, f"{RG2}={REL}"]
    # homelab#2478 2b: later appends them to Later in the order given.
    resp = client.post("/mcp-api/run/request_album", headers=WRITE, json={"albums": [
        {"release_group": RG2}, {"release_group": RG}], "later": True})
    assert resp.status_code == 200, resp.json
    assert env["commands"].ran[-1] == ["music-requests", "add", "--later", "--", RG2, RG]
    for albums in ([], [{"release_group": "abc"}], [{"release_group": RG, "surprise": 1}],
                   [{"release_group": RG}, {"release_group": RG, "preferred_release": REL}],
                   [{"release_group": RG, "preferred_release": f"https://musicbrainz.org/release-group/{RG}"}],
                   [{"release_group": f"{i:08d}-2d38-3d3c-9d38-4f0c2b5b7a11"} for i in range(26)], ["x"]):
        assert client.post("/mcp-api/run/request_album", json={"albums": albums},
                           headers=WRITE).status_code == 400, albums
    assert client.post("/mcp-api/run/request_album", json={"albums": [{"release_group": RG}], "later": "yes"},
                       headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/request_album", json={"albums": [{"release_group": RG}]},
                       headers=READ).status_code == 403


def test_search_releases_passes_the_query_after_a_double_dash(client, env):
    """homelab#2478 2c: a read; the query is never a flag."""
    resp = client.post("/mcp-api/run/search_releases", headers=READ, json={"query": "  --apply  band \n album "})
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"query": "--apply band album", "limit": 5}
    assert env["commands"].ran[-1] == ["music-requests", "search", "--limit=5", "--", "--apply band album"]
    for body in ({}, {"query": ""}, {"query": "x" * 201}, {"query": "x", "limit": 0}, {"query": "x", "limit": 11},
                 {"query": "x", "limit": True}, {"query": "x", "apply": True}, {"query": 3}):
        assert client.post("/mcp-api/run/search_releases", json=body, headers=READ).status_code == 400, body


def test_remove_from_later_normalises_its_release_groups(client, env):
    """homelab#2478 2b: release groups as MBIDs or URLs, deduplicated in order, and a position."""
    resp = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={
        "release_groups": [RG2, f"https://musicbrainz.org/release-group/{RG}", RG], "through": 3})
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"release_groups": [RG2, RG], "albums": [], "through": 3}
    assert env["commands"].ran[-1] == ["music-later", "remove", "--through=3", "--", RG2, RG]
    resp = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={"through": 1})
    assert env["commands"].ran[-1] == ["music-later", "remove", "--through=1"]
    for body in ({}, {"release_groups": []}, {"through": 0}, {"through": True}, {"release_groups": "x"},
                 {"release_groups": ["abc"]}, {"release_groups": [RG], "surprise": 1}, {"albums": "pastel"},
                 {"albums": [""]}, {"albums": [3]}, {"albums": ["pastel"] * 26}):
        assert client.post("/mcp-api/run/remove_from_later", json=body, headers=WRITE).status_code == 400, body
    assert client.post("/mcp-api/run/remove_from_later", json={"through": 1}, headers=READ).status_code == 403


def test_remove_from_later_resolves_its_albums(client, env):
    """homelab#2534: an album query, resolved to its key as find_albums resolves it."""
    resp = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={
        "albums": ["pastel", "Nina Simone — Pastel Blues", "ALB3"]})
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"release_groups": [], "albums": ["ALB3"], "through": None}
    assert env["commands"].ran[-1] == ["music-later", "remove", "--album=ALB3"]
    before = len(env["commands"].ran)
    amb = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={"albums": ["pastel", "abbey"]})
    assert amb.status_code == 200 and amb.json["status"] == "ambiguous" and len(amb.json["candidates"]) == 2
    missing = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={"albums": ["zzz nothing"]})
    assert missing.json["status"] == "not_found"
    assert len(env["commands"].ran) == before


@pytest.fixture
def state_down(monkeypatch):
    """The music database, which holds the state, is down."""
    from music_fetch import state

    def down(url=None):
        raise state.Unavailable("the music database is unreachable")

    monkeypatch.setattr(state, "connect", down)


def test_get_later_needs_the_database(client, state_down):
    resp = client.get("/mcp-api/later", headers=READ)
    assert resp.status_code == 503 and resp.get_json()["status"] == "unavailable"


def test_list_requests_needs_the_database(client, state_down):
    resp = client.get("/mcp-api/requests", headers=READ)
    assert resp.status_code == 503 and resp.get_json()["status"] == "unavailable"
    assert client.get("/mcp-api/requests?status=lost", headers=READ).status_code == 400


def test_bad_arguments_are_400(client):
    assert client.post("/mcp-api/run/canon_albums", json={"mb_budget": -1}, headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/embed_covers", json={"surprise": 1}, headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/embed_covers", json=[1], headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/nope", json={}, headers=WRITE).status_code == 404


def test_replace_item_passes_the_canonical_watch_url(client, env):
    resp = client.post("/mcp-api/run/replace_item",
                       json={"id": 1, "youtube_url": "https://youtu.be/dQw4w9WgXcQ?si=abc&t=3"}, headers=WRITE)
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"id": 1, "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    assert env["commands"].ran[-1] == ["music-audit-lengths", "--replace", "1", "--youtube",
                                       "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    for bad in ["https://evil.example/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/playlist?list=PL1", "", 5]:
        assert client.post("/mcp-api/run/replace_item", json={"id": 1, "youtube_url": bad},
                           headers=WRITE).status_code == 400, bad


def test_unlink_spotify_id_reduces_a_track_url_to_its_id(client, env):
    resp = client.post("/mcp-api/run/unlink_spotify_id",
                       json={"id": 9674, "track": "https://open.spotify.com/track/2o49Twc3qrNMOt8gq9W06L?si=x"},
                       headers=WRITE)
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"id": 9674, "track": "2o49Twc3qrNMOt8gq9W06L"}
    assert env["commands"].ran[-1] == ["music-audit-dupes", "--unlink", "9674", "2o49Twc3qrNMOt8gq9W06L"]
    for bad in [{"id": 9674, "track": "--apply"}, {"id": 9674, "track": "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN"},
                {"id": 0, "track": "2o49Twc3qrNMOt8gq9W06L"}, {"id": 9674}, {"id": 9674, "track": "2o49Twc3qrNMOt8gq9W06L", "x": 1}]:
        assert client.post("/mcp-api/run/unlink_spotify_id", json=bad, headers=WRITE).status_code == 400, bad


def test_set_recording_reduces_a_recording_url_to_its_mbid(client, env):
    rec = "dd51202e-05ac-4bc9-b597-e117f81c47f8"
    resp = client.post("/mcp-api/run/set_recording",
                       json={"id": 4230, "recording": f"https://musicbrainz.org/recording/{rec.upper()}"}, headers=WRITE)
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"id": 4230, "recording": rec}
    assert env["commands"].ran[-1] == ["music-mb-fingerprint", "--set-recording", "4230", rec]
    for bad in [{"id": 4230, "recording": "--apply"}, {"id": 4230, "recording": f"https://musicbrainz.org/release/{rec}"},
                {"id": 0, "recording": rec}, {"id": 4230}, {"id": 4230, "recording": rec, "x": 1}]:
        assert client.post("/mcp-api/run/set_recording", json=bad, headers=WRITE).status_code == 400, bad


def test_album_urls_resolve_like_the_cli(client):
    # An album URL with no valid ID is refused, not searched for as text.
    resp = client.post("/mcp-api/run/reset_album", json={"query": "https://open.spotify.com/album/ALB3"}, headers=WRITE)
    assert resp.status_code == 400
    real = "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN?si=x"
    resp = client.post("/mcp-api/run/requeue_albums", json={"ids": [real]}, headers=WRITE)
    assert resp.json["args"]["ids"] == ["0ETFjACtuP2ADo6LFhL6HN"]
    assert client.post("/mcp-api/run/requeue_albums", json={"ids": ["https://open.spotify.com/track/0ETFjACtuP2ADo6LFhL6HN"]},
                       headers=WRITE).status_code == 400


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_a_slow_job_returns_its_id_and_get_job_pages_its_output(client, env, monkeypatch):
    release = threading.Event()

    def slow(argv, env_, on_line):
        for n in range(250):
            on_line(f"line {n}")
        release.wait(5)
        return 0

    env["jobs"]._run_command = slow
    monkeypatch.setenv("MCP_INLINE_SECONDS", "0.2")
    resp = client.post("/mcp-api/run/audit_want_albums", json={}, headers=READ)
    assert resp.status_code == 202 and resp.json["status"] == "running"
    job_id = resp.json["job_id"]
    release.set()
    env["jobs"].wait(job_id, 5)
    first = client.get(f"/mcp-api/jobs/{job_id}", headers=MCP).json
    assert first["status"] == "done" and first["total_lines"] == 250 and first["next_offset"] == 200
    rest = client.get(f"/mcp-api/jobs/{job_id}?offset=200", headers=MCP).json
    assert rest["output"].splitlines()[0] == "line 200" and rest["next_offset"] is None


def test_no_read_returns_a_credential(client, env, no_event_store):
    """Every GET route, and a run's job view, over a store and job output holding an API key (#343)."""
    url = f"http://prowlarr:9696/1/download?apikey={mcp_harness.SECRET}&link=x"

    def leaky(argv, env_, on_line):
        on_line(f"[ERR]  grab failed: Max retries exceeded with url: {url}")
        return 0

    env["jobs"]._run_command = leaky
    ran = client.post("/mcp-api/run/audit_want_albums", json={}, headers=READ)
    reads = {
        "/mcp-api/library_status": ["/mcp-api/library_status"],
        "/mcp-api/albums": ["/mcp-api/albums?query=abbey"],
        "/mcp-api/album": ["/mcp-api/album?query=ALB1", "/mcp-api/album?query=abbey"],
        "/mcp-api/items/<int:item_id>": [f"/mcp-api/items/{env['items'][0].id}"],
        "/mcp-api/quarantine": ["/mcp-api/quarantine?kind=unmatched"],
        "/mcp-api/playlists/<name>": ["/mcp-api/playlists/chill"],
        "/mcp-api/album_history": ["/mcp-api/album_history?query=ALB1"],
        "/mcp-api/find_events": ["/mcp-api/find_events?kind=failure&data=true"],
        "/mcp-api/digest_summary": ["/mcp-api/digest_summary"],
        "/mcp-api/overrides": ["/mcp-api/overrides"],
        "/mcp-api/requests": ["/mcp-api/requests"],
        "/mcp-api/later": ["/mcp-api/later"],
        "/mcp-api/jobs/<job_id>": [f"/mcp-api/jobs/{ran.json['job_id']}"],
    }
    routes = {r.rule for r in client.application.url_map.iter_rules()
              if r.rule.startswith("/mcp-api/") and "GET" in r.methods}
    assert set(reads) == routes, "a new read route: add it here"
    for path in [p for paths in reads.values() for p in paths]:
        assert mcp_harness.SECRET not in client.get(path, headers=READ).get_data(as_text=True), path
    assert mcp_harness.SECRET not in ran.get_data(as_text=True)
    album = client.get("/mcp-api/album?query=ALB1", headers=READ).json["album"]["record"]
    assert "download_url" not in album["candidate"] and album["candidate"]["guid"] == "https://indexer/details/9"
    assert album["blocklist"] == ["https://indexer/api?t=get&id=8&apikey=REDACTED"]
    assert "apikey=REDACTED" in client.get(f"/mcp-api/jobs/{ran.json['job_id']}", headers=READ).json["output"]


def test_unknown_job(client):
    resp = client.get("/mcp-api/jobs/deadbeef", headers=MCP)
    assert resp.status_code == 404 and resp.json["status"] == "unknown"
    assert "pruned or lost to a restart" in resp.json["error"]


def test_a_full_queue_returns_busy_with_the_queue(client, env):
    with patch.object(env["jobs"], "submit", side_effect=mcp_jobs.Busy([{"job_id": "j1"}])):
        resp = client.post("/mcp-api/run/audit_lengths", json={}, headers=READ)
    assert resp.status_code == 503 and resp.json == {"status": "busy", "queue": [{"job_id": "j1"}]}


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_trigger_scan_is_a_no_op_while_one_is_pending(client):
    with patch("music_service.prefect_client.pending_state", return_value="RUNNING"), \
         patch("music_service.prefect_client.trigger_scan") as submit:
        resp = client.post("/mcp-api/trigger/scan", headers=WRITE)
    assert resp.json == {"status": "already_pending", "flow": "music-scan", "state": "RUNNING"}
    submit.assert_not_called()


def test_trigger_album_tick_submits(client):
    with patch("music_service.prefect_client.pending_state", return_value=None), \
         patch("music_service.prefect_client.trigger_album_tick", return_value=True) as submit:
        resp = client.post("/mcp-api/trigger/album_tick", headers=WRITE)
    assert resp.json == {"status": "submitted", "flow": "music-albums"}
    submit.assert_called_once()


def test_trigger_reports_an_unreachable_prefect(client):
    with patch("music_service.prefect_client.pending_state", side_effect=OSError("down")):
        assert client.post("/mcp-api/trigger/scan", headers=WRITE).status_code == 503


# ---------------------------------------------------------------------------
# History (homelab#2413)
# ---------------------------------------------------------------------------

TEST_DB = os.environ.get("MUSIC_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not TEST_DB, reason="MUSIC_TEST_DATABASE_URL is unset")
RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"


@pytest.fixture
def no_event_store(monkeypatch):
    """The event store can't be read, while the state still can."""
    from music_service import mcp_history

    def unavailable():
        raise mcp_history.Unavailable("the event store is unavailable: OperationalError")

    monkeypatch.setattr(mcp_history, "connect", unavailable)


@pytest.fixture
def event_store(monkeypatch):
    """The events module, recording into the test's database (the state's)."""
    from music_fetch import events

    monkeypatch.setattr(events.metrics, "_push", lambda body, job: None)
    events._reset()
    yield events
    events._reset()


def test_album_history_without_the_event_store_is_unavailable(client, no_event_store):
    resp = client.get("/mcp-api/album_history?query=ALB1", headers=READ)
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "unavailable" and "event store is unavailable" in resp.get_json()["error"]
    # The album resolves before the store is read.
    assert client.get("/mcp-api/album_history?query=abbey", headers=READ).get_json()["status"] == "ambiguous"
    assert client.get("/mcp-api/album_history?query=ALB1&since=yesterday", headers=READ).status_code == 400


def test_digest_summary_without_the_event_store_still_reports_the_issues(client, no_event_store):
    resp = client.get("/mcp-api/digest_summary", headers=READ)
    body = resp.get_json()
    assert resp.status_code == 200 and body["status"] == "ok"
    assert "event store is unavailable" in body["history"]["error"]
    issues = body["issues"]
    assert issues["mixed"] == {"total": 1, "listed": [{"key": "ALB1", "album": "The Beatles — Abbey Road",
                                                       "origin": "album"}]}
    assert [a["key"] for a in issues["stuck"]["failed"]["listed"]] == ["ALB3"]
    assert issues["stuck"]["missing"]["total"] == issues["stuck"]["grabbed"]["total"] == 0
    # The seeded replaced files have no tags: nothing says the library holds them.
    assert issues["only_copies"] == {"total": 2, "listed": ["replaced/3-three.m4a", "replaced/4-four.m4a"]}
    assert issues["path_clashes"] == {"total": 0, "listed": []}
    assert issues["empty_slots"]["playlists"] == {"chill": {"total": 1, "entries": ["Someone — Absent"]}}
    assert issues["rejects"] == {"total": 1, "listed": ["rejected/chill/two.m4a"]}
    assert body["requests"] == {"missing": {"total": 0, "listed": []}, "failed": {"total": 0, "listed": []},
                                "fallback": {"total": 0, "listed": []}}
    assert body["since"].endswith("Z") and body["until"].endswith("Z")


def test_digest_summary_lists_path_clashes(env, client, no_event_store):
    with sqlite3.connect(env["db"]) as conn:
        conn.execute("INSERT INTO item_attributes (entity_id, key, value) VALUES (?, 'canon_clash', '1')",
                     (env["items"][0].id,))
    clashes = client.get("/mcp-api/digest_summary", headers=READ).get_json()["issues"]["path_clashes"]
    assert clashes["total"] == 1 and clashes["listed"][0]["id"] == env["items"][0].id
    assert clashes["listed"][0]["path"] == "The Beatles/Abbey Road/01 - Come Together.m4a"


@needs_pg
def test_album_history_reads_an_albums_events_newest_first(env, client, event_store, monkeypatch):
    from music_fetch import albums

    state = albums.State.load()
    state.albums["ALB1"]["release_files"] = {"mb": {"mb_releasegroupid": RG}}
    state.save()
    event_store.record("grab", "[GRAB] one", source="albums", spotify_album_id="ALB1")
    event_store.record("retire", "[RETIRE] two", source="release", release_group=RG, item_id=7)
    event_store.record("grab", "[GRAB] other album", source="albums", spotify_album_id="ALB2")
    event_store.record("import_done", "[DONE] three", source="albums", spotify_album_id="ALB1", release_group=RG)

    body = client.get("/mcp-api/album_history?query=ALB1", headers=READ).get_json()
    assert body["status"] == "ok" and body["album"]["release_group"] == RG
    assert [e["message"] for e in body["events"]] == ["[DONE] three", "[RETIRE] two", "[GRAB] one"]
    assert body["total"] == 3 and body["next_offset"] is None
    assert body["events"][1]["item_id"] == 7 and body["events"][0]["at"].endswith("Z")
    future = client.get("/mcp-api/album_history?query=ALB1&since=2999-01-01T00:00:00Z", headers=READ).get_json()
    assert future["total"] == 0 and future["since"] == "2999-01-01T00:00:00Z"


@needs_pg
def test_album_history_of_a_dropped_album_reads_its_events(client, event_store):
    """homelab#2544: the wants feed dropped album 611's record; its key still finds its events."""
    event_store.record("album_dropped", "[UNWANTED] 611 Bob Dylan — Vol. 5: record dropped", source="albums",
                       spotify_album_id="GONE1", release_group=RG, data={"key": "611", "status": "wanted"})
    for query in ("611", "GONE1", RG):
        body = client.get(f"/mcp-api/album_history?query={query}", headers=READ).get_json()
        assert body["status"] == "gone" and body["album"] is None, query
        assert [e["kind"] for e in body["events"]] == ["album_dropped"], query
    resp = client.get("/mcp-api/album_history?query=612", headers=READ)
    assert resp.status_code == 404 and resp.get_json()["status"] == "not_found"


@needs_pg
def test_digest_summary_counts_and_lists_the_windows_events(client, event_store):
    for n in range(30):
        event_store.record("retire", f"[RETIRE] {n}", source="release", item_id=n)
    event_store.record("failure", "[FAIL] gave up", source="albums", data={"gave_up": True})
    history = client.get("/mcp-api/digest_summary", headers=READ).get_json()["history"]
    assert history["counts"] == {"failure": 1, "retire": 30}
    retired = [e["message"] for e in history["events"]["retire"]]
    assert retired == [f"[RETIRE] {n}" for n in range(5, 30)]  # the latest 25, oldest first
    assert history["events"]["failure"] == [{"at": history["events"]["failure"][0]["at"],
                                             "message": "[FAIL] gave up", "gave_up": True}]
    event_store.record("merge", "[SAME] x", source="import", item_id=9, data={"why": "same recording at import"})
    merged = client.get("/mcp-api/digest_summary", headers=READ).get_json()["history"]["events"]["merge"]
    assert merged[0]["item_id"] == 9 and merged[0]["data"] == {"why": "same recording at import"}
    absorbed = {"absorbed": [{"key": "1184", "album": "A — X (Expanded)", "spotify_album_ids": ["SP2"]}],
                "album": "A — X", "key": "1781"}
    event_store.record("album_merged", "[MERGE] A — X (1781) ← A — X (Expanded) (1184) as RG", source="resolve",
                       data=absorbed)
    [album_merged] = client.get("/mcp-api/digest_summary", headers=READ).get_json()["history"]["events"]["album_merged"]
    assert album_merged["data"] == absorbed  # homelab#2607: the digest drops the moves it explains
    later = client.get("/mcp-api/digest_summary?since=2999-01-01T00:00:00Z", headers=READ).get_json()
    assert later["history"] == {"counts": {}, "events": {}}


@needs_pg
def test_event_history_returns_no_credential(env, client, event_store):
    reason = f"Max retries exceeded with url: /1/download?apikey={mcp_harness.SECRET}"
    event_store.record("failure", f"[FAIL] Abbey Road: {reason}", source="albums", spotify_album_id="ALB1",
                       data={"reason": reason, "gave_up": True})
    for path in ["/mcp-api/album_history?query=ALB1", "/mcp-api/digest_summary",
                 "/mcp-api/find_events?kind=failure", "/mcp-api/find_events?kind=failure&data=true"]:
        resp = client.get(path, headers=READ)
        assert resp.status_code == 200 and mcp_harness.SECRET not in resp.get_data(as_text=True), path


def _backdate(message: str, at: str) -> None:
    from music_fetch import db

    with db.connect() as conn:
        conn.execute("UPDATE events SET at = %s::timestamptz WHERE message = %s", (at, message))


def test_find_events_without_the_event_store_is_unavailable(client, no_event_store):
    resp = client.get("/mcp-api/find_events?kind=import_incomplete", headers=READ)
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "unavailable" and "event store is unavailable" in resp.get_json()["error"]


@pytest.mark.parametrize(("query", "error"), [
    ("", "kind is required"),
    ("kind=import_incomplete,imported_maybe", "unknown event kind(s) imported_maybe"),
    ("kind=grab&since=yesterday", "since is not ISO 8601"),
    ("kind=grab&until=tomorrow", "until is not ISO 8601"),
    ("kind=grab&item_id=x", "item_id must be an integer"),
    ("kind=grab&data=maybe", "data must be true or false"),
    ("kind=grab&offset=-", "offset must be"),
])
def test_find_events_refuses_bad_arguments(client, no_event_store, query, error):
    """Refused before the event store is read: no_event_store would answer 503."""
    resp = client.get(f"/mcp-api/find_events?{query}", headers=READ)
    assert resp.status_code == 400 and error in resp.get_json()["error"], resp.get_json()


@needs_pg
def test_find_events_lists_kinds_across_albums_newest_first(client, event_store):
    event_store.record("import_incomplete", "[INCOMPLETE] old", source="albums", spotify_album_id="ALB1",
                       data={"key": 611, "missing": 2})
    event_store.record("grab", "[GRAB] one", source="albums", spotify_album_id="ALB2", data={"key": "612"})
    event_store.record("import_incomplete", "[INCOMPLETE] new", source="albums", release_group=RG,
                       data={"key": "613", "missing": 1})
    event_store.record("retire", "[RETIRE] other kind", source="release", item_id=7)
    _backdate("[INCOMPLETE] old", "2026-10-01T12:00:00Z")
    _backdate("[GRAB] one", "2026-10-02T12:00:00Z")
    _backdate("[INCOMPLETE] new", "2026-10-03T12:00:00Z")

    body = client.get("/mcp-api/find_events?kind=import_incomplete&since=2026-09-01T00:00:00Z",
                      headers=READ).get_json()
    assert body["status"] == "ok" and body["kinds"] == ["import_incomplete"]
    assert body["counts"] == {"import_incomplete": 2} and body["total"] == 2 and body["next_offset"] is None
    assert body["since"] == "2026-09-01T00:00:00Z" and body["until"].endswith("Z")
    assert body["events"] == [
        {"at": "2026-10-03T12:00:00Z", "kind": "import_incomplete", "source": "albums", "key": "613",
         "spotify_album_id": None, "release_group": RG, "item_id": None, "caller": None, "job_id": None,
         "message": "[INCOMPLETE] new"},
        {"at": "2026-10-01T12:00:00Z", "kind": "import_incomplete", "source": "albums", "key": "611",
         "spotify_album_id": "ALB1", "release_group": None, "item_id": None, "caller": None, "job_id": None,
         "message": "[INCOMPLETE] old"},
    ]
    # Several kinds, repeated or comma-separated; data only when asked.
    both = client.get("/mcp-api/find_events?kind=grab&kind=import_incomplete&since=2026-09-01T00:00:00Z&data=true",
                      headers=READ).get_json()
    assert both["counts"] == {"grab": 1, "import_incomplete": 2}
    assert [e["message"] for e in both["events"]] == ["[INCOMPLETE] new", "[GRAB] one", "[INCOMPLETE] old"]
    assert both["events"][0]["data"] == {"key": "613", "missing": 1}
    assert client.get("/mcp-api/find_events?kind=grab,import_incomplete&since=2026-09-01T00:00:00Z",
                      headers=READ).get_json()["counts"] == both["counts"]
    # [since, until): the until bound is left out.
    window = client.get("/mcp-api/find_events?kind=grab,import_incomplete&since=2026-10-02T00:00:00Z"
                        "&until=2026-10-03T12:00:00Z", headers=READ).get_json()
    assert window["counts"] == {"grab": 1} and window["until"] == "2026-10-03T12:00:00Z"
    # The default window is the last 7 days: only the retire recorded now.
    recent = client.get("/mcp-api/find_events?kind=retire,import_incomplete", headers=READ).get_json()
    assert recent["counts"] == {"retire": 1} and recent["events"][0]["item_id"] == 7
    # The cheap filters: item ID and source.
    assert client.get("/mcp-api/find_events?kind=retire&item_id=8", headers=READ).get_json()["total"] == 0
    assert client.get("/mcp-api/find_events?kind=retire&item_id=7&source=release",
                      headers=READ).get_json()["total"] == 1
    assert client.get("/mcp-api/find_events?kind=retire&source=albums", headers=READ).get_json()["counts"] == {}


@needs_pg
def test_find_events_pages_and_counts_the_whole_window(client, event_store):
    from music_service import mcp_reads

    many = mcp_reads.PAGE_ENTRIES + 30
    for n in range(many):
        event_store.record("search_miss", f"[MISS] {n}", source="albums", data={"key": str(n), "why": "x" * 50})
    event_store.record("grab", "[GRAB] one", source="albums")

    def every(query):
        pages, offset = [], 0
        while offset is not None:
            page = client.get(f"/mcp-api/find_events?{query}&offset={offset}", headers=READ).get_json()
            assert page["events"] and page["offset"] == offset
            # Every page counts the whole window, not itself.
            assert page["counts"] == {"grab": 1, "search_miss": many} and page["total"] == many + 1
            pages.append(page)
            offset = page["next_offset"]
        return pages

    plain = every("kind=search_miss,grab")
    assert len(plain) > 1 and plain[0]["next_offset"] == len(plain[0]["events"])
    events = [e for page in plain for e in page["events"]]
    assert all("data" not in e for e in events)
    assert [e["message"] for e in events] == ["[GRAB] one"] + [f"[MISS] {n}" for n in range(many - 1, -1, -1)]
    # With data each event is larger, so a page holds fewer (about PAGE_BYTES), and paging still covers every one.
    full = every("kind=search_miss,grab&data=true")
    assert len(full[0]["events"]) < len(plain[0]["events"])
    assert [e["key"] for page in full for e in page["events"]] == [None] + [str(n) for n in range(many - 1, -1, -1)]
    assert full[0]["events"][1]["data"] == {"key": str(many - 1), "why": "x" * 50}


@needs_pg
def test_list_requests_and_the_digest_show_why_a_fallbacks_downloads_were_rejected(env, client, tmp_path,
                                                                                    monkeypatch):
    """homelab#2546: a fallback whose every reject is AcoustID knowing no recording says so."""
    from music_fetch import album_requests as req
    from music_fetch import albums
    from music_fetch import state as statedb
    from music_scan import guard

    recs = [f"0000000{i}-0000-4000-8000-00000000000{i}" for i in (1, 2, 3)]
    with statedb.session() as conn:
        assert req.add(conn, req.Tracklist(RG, "b84e6c2a-2d38-3d3c-9d38-4f0c2b5b7a11", "Band", "Transa",
                                           [["T", "Band", None, None, 1, n] for n in (1, 2, 3)]), "sub-1")
    state = albums.State.load()
    state.albums["REQ1"] = {**statedb.new_record("REQ1"), "artist": "Band", "name": "Transa",
                            "status": albums.FALLBACK, "origin": albums.REQUEST, "release_group": RG,
                            "fallback_at": "2026-10-31T00:00:00+00:00", "fallback_tracks": {req.SOURCE: recs}}
    state.save()
    held = tmp_path / "quarantine" / "rejected" / req.SOURCE
    held.mkdir(parents=True)
    urls = {}
    for n, rec in enumerate(recs[:2]):  # the third: spotdl found nothing
        (held / f"{n}.m4a").write_bytes(b"x")
        (held / f"{n}.m4a.reason").write_text(f"acoustid-unknown: not recording {rec}: …\n")
        urls[f"{n}.m4a"] = req.recording_url(rec)
    monkeypatch.setattr(guard, "song_url", lambda path: urls.get(Path(path).name))

    [listed] = client.get("/mcp-api/requests", headers=READ).get_json()["requests"]
    assert listed["status"] == "fallback"
    assert listed["album"]["fallback_left"] == 3
    assert listed["album"]["fallback_rejected"] == {"acoustid-unknown": 2}
    assert listed["album"]["acoustid_unknown_only"] is True
    [digested] = client.get("/mcp-api/digest_summary", headers=READ).get_json()["requests"]["fallback"]["listed"]
    assert digested["album"] == listed["album"]

    # One download AcoustID named as another recording: no longer unknown only.
    (held / "1.m4a.reason").write_text(f"fingerprint: not recording {recs[1]}: …\n")
    album = client.get("/mcp-api/requests", headers=READ).get_json()["requests"][0]["album"]
    assert album["fallback_rejected"] == {"acoustid-unknown": 1, "fingerprint": 1}
    assert album["acoustid_unknown_only"] is False


def test_stuck_names_stale_grabs_and_fallbacks_only():
    from datetime import datetime, timezone

    from music_service import mcp_history

    now = datetime(2026, 10, 29, 5, tzinfo=timezone.utc)
    records = {
        "G1": {"artist": "A", "name": "Old grab", "status": "grabbed", "grabbed_at": "2026-10-26T00:00:00+00:00"},
        # 17 h: SABnzbd's URL grabber hung on its NZB fetch (homelab#2516).
        "G3": {"artist": "A", "name": "Hung grab", "status": "grabbed", "grabbed_at": "2026-10-28T12:00:00+00:00"},
        # 7 h: a MusicBrainz outage's import deferrals, still within their budget.
        "G2": {"artist": "A", "name": "New grab", "status": "grabbed", "grabbed_at": "2026-10-28T22:00:00+00:00"},
        "F1": {"artist": "B", "name": "Old fallback", "status": "fallback", "fallback_at": "2026-10-01T00:00:00+00:00",
               "fallback_tracks": {"later": ["s1", "s2"]}},
        "F2": {"artist": "B", "name": "New fallback", "status": "fallback", "fallback_at": "2026-10-25T00:00:00+00:00"},
        "M1": {"artist": "C", "name": "Missing", "status": "missing", "origin": "wants"},
        "W1": {"artist": "D", "name": "Wanted", "status": "wanted"},
    }
    out = mcp_history.stuck(records, now)
    assert [a["key"] for a in out["grabbed"]["listed"]] == ["G3", "G1"]
    assert out["fallback"]["listed"] == [{"key": "F1", "album": "B — Old fallback", "origin": "album",
                                          "fallback_at": "2026-10-01T00:00:00+00:00", "tracks": 2}]
    assert out["missing"]["listed"][0]["origin"] == "wants"
    assert out["failed"] == {"total": 0, "listed": []}

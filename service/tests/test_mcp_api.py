"""Tests for the /mcp-api/* routes (homelab#2412), against a real beets library."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

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


# ---------------------------------------------------------------------------
# Structured reads
# ---------------------------------------------------------------------------


def test_library_status(client):
    body = client.get("/mcp-api/library_status", headers=MCP).json
    assert body["items"] == 3
    assert body["albums"] == {"total": 3, "by_status": {"imported": 1, "wanted": 1, "failed": 1}}
    assert body["quarantine"] == {"unmatched": 1, "rejected": 1, "replaced": 2}
    assert body["playlists"] == ["chill"]
    assert body["jobs"] == {"running": None, "queued": 0, "depth": 5}


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


def test_remove_from_later_normalises_its_release_groups(client, env):
    """homelab#2478 2b: release groups as MBIDs or URLs, deduplicated in order, and a position."""
    resp = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={
        "release_groups": [RG2, f"https://musicbrainz.org/release-group/{RG}", RG], "through": 3})
    assert resp.status_code == 200, resp.json
    assert resp.json["args"] == {"release_groups": [RG2, RG], "through": 3}
    assert env["commands"].ran[-1] == ["music-later", "remove", "--through=3", "--", RG2, RG]
    resp = client.post("/mcp-api/run/remove_from_later", headers=WRITE, json={"through": 1})
    assert env["commands"].ran[-1] == ["music-later", "remove", "--through=1"]
    for body in ({}, {"release_groups": []}, {"through": 0}, {"through": True}, {"release_groups": "x"},
                 {"release_groups": ["abc"]}, {"release_groups": [RG], "surprise": 1}):
        assert client.post("/mcp-api/run/remove_from_later", json=body, headers=WRITE).status_code == 400, body
    assert client.post("/mcp-api/run/remove_from_later", json={"through": 1}, headers=READ).status_code == 403


def test_get_later_needs_the_state_in_the_database(client):
    resp = client.get("/mcp-api/later", headers=READ)
    assert resp.status_code == 503 and resp.get_json()["status"] == "unavailable"


def test_list_requests_needs_the_state_in_the_database(client):
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
    from music_fetch import db, events

    monkeypatch.delenv(db.ENV, raising=False)
    events._reset()


@pytest.fixture
def event_store(monkeypatch):
    """A fresh database on the test server as MUSIC_DATABASE_URL (as fetch_tests/test_events.py)."""
    import uuid

    import psycopg

    from music_fetch import db, events

    name = f"music_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DB, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    monkeypatch.setenv(db.ENV, psycopg.conninfo.make_conninfo(TEST_DB, dbname=name))
    monkeypatch.setattr(events.metrics, "_push", lambda body, job: None)
    events._reset()
    yield events
    events._reset()
    with psycopg.connect(TEST_DB, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_album_history_without_the_event_store_is_unavailable(client, no_event_store):
    resp = client.get("/mcp-api/album_history?query=ALB1", headers=READ)
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "unavailable" and "MUSIC_DATABASE_URL" in resp.get_json()["error"]
    # The album resolves before the store is read.
    assert client.get("/mcp-api/album_history?query=abbey", headers=READ).get_json()["status"] == "ambiguous"
    assert client.get("/mcp-api/album_history?query=ALB1&since=yesterday", headers=READ).status_code == 400


def test_digest_summary_without_the_event_store_still_reports_the_issues(client, no_event_store):
    resp = client.get("/mcp-api/digest_summary", headers=READ)
    body = resp.get_json()
    assert resp.status_code == 200 and body["status"] == "ok"
    assert "MUSIC_DATABASE_URL" in body["history"]["error"]
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
    assert body["requests"] == {"missing": {"total": 0, "listed": []}, "failed": {"total": 0, "listed": []}}
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
    import json as _json

    from music_fetch import albums

    state = _json.loads(albums.STATE_FILE.read_text())
    state["albums"]["ALB1"]["release_files"] = {"mb": {"mb_releasegroupid": RG}}
    albums.STATE_FILE.write_text(_json.dumps(state))
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
    later = client.get("/mcp-api/digest_summary?since=2999-01-01T00:00:00Z", headers=READ).get_json()
    assert later["history"] == {"counts": {}, "events": {}}


@needs_pg
def test_event_history_returns_no_credential(env, client, event_store):
    reason = f"Max retries exceeded with url: /1/download?apikey={mcp_harness.SECRET}"
    event_store.record("failure", f"[FAIL] Abbey Road: {reason}", source="albums", spotify_album_id="ALB1",
                       data={"reason": reason, "gave_up": True})
    for path in ["/mcp-api/album_history?query=ALB1", "/mcp-api/digest_summary"]:
        resp = client.get(path, headers=READ)
        assert resp.status_code == 200 and mcp_harness.SECRET not in resp.get_data(as_text=True), path


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

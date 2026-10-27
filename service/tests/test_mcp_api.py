"""Tests for the /mcp-api/* routes (homelab#2412), against a real beets library."""

from __future__ import annotations

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


def test_bad_arguments_are_400(client):
    assert client.post("/mcp-api/run/canon_albums", json={"mb_budget": -1}, headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/embed_covers", json={"surprise": 1}, headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/embed_covers", json=[1], headers=WRITE).status_code == 400
    assert client.post("/mcp-api/run/nope", json={}, headers=WRITE).status_code == 404


def test_replace_item_waits_for_2411(client):
    resp = client.post("/mcp-api/run/replace_item", json={"id": 1, "youtube_url": "dQw4w9WgXcQ"}, headers=WRITE)
    assert resp.status_code == 501 and resp.json["status"] == "not_implemented"


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

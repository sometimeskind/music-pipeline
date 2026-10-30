"""The adapter against the real Flask app (service/tests/mcp_harness.py), over HTTP.

The pipeline runs as its own process in the service's Python environment, so the adapter's
dependencies (fastmcp) and the service's (prefect, beets) never share one. Point
MUSIC_MCP_CONTRACT_PYTHON at a Python with the service installed (CI: the runner's, after
the unit tests' install). Without it these tests skip, unless MUSIC_MCP_CONTRACT_REQUIRED=1
(CI sets it, so a broken setup fails instead of skipping silently).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from prometheus_client import REGISTRY

from music_mcp.auth import Caller
from music_mcp.pipeline import Pipeline
from music_mcp.server import build_server

HARNESS = Path(__file__).resolve().parents[2] / "service" / "tests" / "mcp_harness.py"
PYTHON = os.environ.get("MUSIC_MCP_CONTRACT_PYTHON")
TOKEN = "mcp-contract-token"
TOM = Caller("sub-tom", "write")
PILOT = Caller("pilot", "read")
RUNNING = {"queued", "waiting_for_lock", "running"}

if not PYTHON:
    if os.environ.get("MUSIC_MCP_CONTRACT_REQUIRED") == "1":
        raise RuntimeError("MUSIC_MCP_CONTRACT_REQUIRED=1 but MUSIC_MCP_CONTRACT_PYTHON is unset")
    pytest.skip("MUSIC_MCP_CONTRACT_PYTHON unset: no service environment to run", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pipeline_url(tmp_path_factory):
    port = _free_port()
    env = os.environ | {"MCP_API_TOKEN": TOKEN, "MCP_INLINE_SECONDS": "5", "PYTHONUNBUFFERED": "1"}
    env.pop("MUSIC_DATABASE_URL", None)  # no event store: the history reads' unavailable path
    proc = subprocess.Popen(
        [PYTHON, str(HARNESS), "--port", str(port), "--dir", str(tmp_path_factory.mktemp("pipeline"))],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 120
        seen: list[str] = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                raise RuntimeError("the harness exited:\n" + "".join(seen))
            seen.append(line)
            if line.startswith("mcp harness on "):
                break
        else:
            raise RuntimeError("the harness did not start:\n" + "".join(seen))
        # The line comes just before waitress binds.
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def server(url: str, caller: Caller, token: str = TOKEN):
    return build_server(Pipeline(url, token), lambda: caller)


async def finish(client: Client, record: dict) -> dict:
    """Poll a job until it leaves the queue."""
    deadline = time.monotonic() + 60
    while record.get("status") in RUNNING:
        assert time.monotonic() < deadline, record
        await asyncio.sleep(0.2)
        record = (await client.call_tool("get_job", {"job_id": record["job_id"]})).data
    return record


async def test_structured_reads(pipeline_url):
    async with Client(server(pipeline_url, PILOT)) as c:
        status = (await c.call_tool("library_status", {})).data
        assert status["status"] == "ok"
        assert status["albums"]["by_status"] == {"imported": 1, "wanted": 1, "failed": 1}
        assert status["quarantine"] == {"unmatched": 1, "rejected": 1, "replaced": 2}
        assert "chill" in status["playlists"]

        found = (await c.call_tool("find_albums", {"query": "abbey"})).data
        assert {a["key"] for a in found["albums"]} == {"ALB1", "ALB2"}

        ambiguous = (await c.call_tool("get_album", {"query": "abbey"})).data
        assert ambiguous["status"] == "ambiguous"
        assert {a["key"] for a in ambiguous["candidates"]} == {"ALB1", "ALB2"}
        assert (await c.call_tool("get_album", {"query": "zzz nothing"})).data["status"] == "not_found"

        album = (await c.call_tool("get_album", {"query": "ALB1"})).data
        assert album["status"] == "ok" and album["album"]["key"] == "ALB1"
        assert "download_url" not in album["album"]["record"]["candidate"]  # Prowlarr's API key (#343)
        assert "apikey=REDACTED" in album["album"]["record"]["blocklist"][0]
        assert album["items"], album
        item_id = album["items"][0]["id"]
        item = (await c.call_tool("get_item", {"id": item_id})).data
        assert item["status"] == "ok" and item["item"]["id"] == item_id
        assert (await c.call_tool("get_item", {"id": 999999})).data["status"] == "not_found"

        for kind, total in [("unmatched", 1), ("rejected", 1), ("replaced", 2)]:
            page = (await c.call_tool("list_quarantine", {"kind": kind})).data
            assert page["status"] == "ok" and page["total"] == total, page
            assert page["next_offset"] is None

        chill = (await c.call_tool("get_playlist", {"name": "chill"})).data
        assert chill["counts"] == {"entries": 4, "resolved": 3, "missing": 1}
        assert (await c.call_tool("get_playlist", {"name": "nope"})).data["status"] == "not_found"

        unknown = (await c.call_tool("get_job", {"job_id": "nope"})).data
        assert unknown["status"] == "unknown"


async def test_history_reads(pipeline_url):
    async with Client(server(pipeline_url, PILOT)) as c:
        history = (await c.call_tool("album_history", {"query": "ALB1"})).data
        assert history["status"] == "unavailable" and history["error"]
        assert (await c.call_tool("album_history", {"query": "abbey"})).data["status"] == "ambiguous"

        digest = (await c.call_tool("digest_summary", {"since": "2026-10-28T05:00:00Z"})).data
        assert digest["status"] == "ok" and digest["since"] == "2026-10-28T05:00:00Z"
        assert "error" in digest["history"]
        issues = digest["issues"]
        assert issues["mixed"]["total"] == 1 and issues["stuck"]["failed"]["total"] == 1
        assert issues["only_copies"]["total"] == 2 and issues["rejects"]["total"] == 1
        assert issues["empty_slots"]["playlists"]["chill"]["total"] == 1
        assert digest["requests"]["missing"]["total"] == 0


async def test_a_read_caller_runs_the_audits(pipeline_url):
    async with Client(server(pipeline_url, PILOT)) as c:
        for tool, args in [("audit_want_albums", {}), ("audit_dupes", {}), ("audit_lengths", {"silence": True}),
                           ("audit_releases", {"replaced_query": ""})]:
            job = await finish(c, (await c.call_tool(tool, args)).data)
            assert job["status"] == "done", job
            assert job["caller"] == "pilot" and job["tier"] == "read"
            assert job["output"].startswith("ran "), job


async def test_the_pipeline_refuses_a_read_tier_write_too(pipeline_url):
    # The adapter refuses first; this is the pipeline's own guard behind it.
    p = Pipeline(pipeline_url, TOKEN)
    try:
        with pytest.raises(ToolError, match="write tier required"):
            await p.post(PILOT, "/run/reset_album", {"query": "pastel"})
    finally:
        await p.aclose()


async def test_dry_run_then_apply(pipeline_url):
    async with Client(server(pipeline_url, TOM)) as c:
        refused = (await c.call_tool("reset_album", {"query": "The Beatles — Abbey Road", "apply": True})).data
        assert refused["status"] == "refused" and refused["reason"] == "no_dry_run"

        dry = await finish(c, (await c.call_tool("reset_album", {"query": "pastel", "reimport": True})).data)
        assert dry["status"] == "done" and dry["mode"] == "dry_run"
        assert dry["args"] == {"album": "ALB3", "reimport": True}
        assert dry["dry_run_hash"]
        # argv is informational: the contract is the normalised args, plus --apply only on an apply.
        assert not any("--apply" in cmd for cmd in dry["argv"])

        applied = await finish(c, (await c.call_tool(
            "reset_album", {"query": "pastel", "reimport": True, "apply": True})).data)
        assert applied["status"] == "done", applied
        assert applied["args"] == dry["args"]
        assert any("--apply" in cmd for cmd in applied["argv"]), applied["argv"]
        assert applied["caller"] == "sub-tom" and applied["tier"] == "write"

        ambiguous = (await c.call_tool("reset_album", {"query": "abbey"})).data
        assert ambiguous["status"] == "ambiguous"


async def test_the_other_write_tools_dry_run(pipeline_url):
    async with Client(server(pipeline_url, TOM)) as c:
        for tool, args, normalised in [
            ("requeue_albums", {"ids": ["ALB3", "ALB2", "ALB3"]}, {"ids": ["ALB2", "ALB3"], "since": None}),
            ("merge_items", {"pairs": [[1, 2]]}, {"pairs": [[1, 2]]}),
            ("embed_covers", {}, {}),
            ("canon_albums", {"mb_budget": 3}, {"refresh": False, "search_budget": None, "mb_budget": 3}),
        ]:
            job = await finish(c, (await c.call_tool(tool, args)).data)
            assert job["status"] == "done", (tool, job)
            assert job["args"] == normalised, (tool, job["args"])
            assert not any("--apply" in cmd for cmd in job["argv"]), (tool, job["argv"])

        # homelab#2411: any accepted form reduces to the canonical watch URL yt-dlp is given.
        replaced = await finish(c, (await c.call_tool(
            "replace_item", {"id": 1, "youtube_url": "https://youtu.be/dQw4w9WgXcQ?t=42"})).data)
        assert replaced["status"] == "done", replaced
        assert replaced["args"] == {"id": 1, "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        assert not any("--apply" in cmd for cmd in replaced["argv"])
        with pytest.raises(ToolError):
            await c.call_tool("replace_item", {"id": 1, "youtube_url": "https://evil.example/watch?v=dQw4w9WgXcQ"})


async def test_the_override_tools(pipeline_url):
    """homelab#2484: the album selector and the track and URL forms normalise as the CLI takes them."""
    async with Client(server(pipeline_url, TOM)) as c:
        for tool, args, normalised in [
            ("set_search_override", {"album": "pastel", "words": "Nina  Simone: Pastel Blues!", "reason": "edition"},
             {"album": "ALB3", "words": "nina simone pastel blues", "reason": "edition"}),
            ("clear_search_override", {"album": "ALB3"}, {"album": "ALB3"}),
            ("hold_album", {"album": "pastel", "reason": " curated\n mix "}, {"album": "ALB3", "reason": "curated mix"}),
            ("release_album", {"album": "pastel"}, {"album": "ALB3"}),
            ("pin_track", {"track": "https://open.spotify.com/track/68ozhSrI4eLbnUgih1SlOT?si=1",
                           "url": "https://music.youtube.com/watch?v=29Sh3yEaBP8&si=abc", "label": "track 1"},
             {"track": "68ozhSrI4eLbnUgih1SlOT", "url": "https://music.youtube.com/watch?v=29Sh3yEaBP8",
              "label": "track 1", "reason": ""}),
            ("unpin_track", {"track": "68ozhSrI4eLbnUgih1SlOT"}, {"track": "68ozhSrI4eLbnUgih1SlOT"}),
        ]:
            job = await finish(c, (await c.call_tool(tool, args)).data)
            assert job["status"] == "done", (tool, job)
            assert job["args"] == normalised, (tool, job["args"])
            assert job["argv"][0][:2] == ["music-overrides", job["argv"][0][1]]
            assert not any("--apply" in cmd for cmd in job["argv"]), (tool, job["argv"])
        applied = await finish(c, (await c.call_tool(
            "hold_album", {"album": "pastel", "reason": "curated mix", "apply": True})).data)
        assert applied["status"] == "done" and any("--apply" in cmd for cmd in applied["argv"]), applied
        assert (await c.call_tool("hold_album", {"album": "abbey", "reason": "x"})).data["status"] == "ambiguous"
        for tool, args in [("hold_album", {"album": "ALB3", "reason": " "}),
                           ("set_search_override", {"album": "ALB3", "words": "〠 ♫"}),
                           ("pin_track", {"track": "ALB3", "url": "https://music.youtube.com/watch?v=x"}),
                           ("pin_track", {"track": "68ozhSrI4eLbnUgih1SlOT",
                                          "url": "https://evil.example/watch?v=29Sh3yEaBP8"}),
                           ("pin_track", {"track": "68ozhSrI4eLbnUgih1SlOT",
                                          "url": "https://music.youtube.com/playlist?list=OLAK5uy_l7iC3z"})]:
            with pytest.raises(ToolError):
                await c.call_tool(tool, args)
    async with Client(server(pipeline_url, PILOT)) as c:
        # No music database here: the read says so.
        listed = (await c.call_tool("list_overrides", {})).data
        assert listed["status"] == "unavailable" and listed["error"]


async def test_the_request_tools(pipeline_url):
    """homelab#2478: request_album's release groups normalise as music-requests takes them."""
    rg, rg2, rel = ("6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11", "0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10",
                    "11111111-1111-4111-8111-111111111111")
    async with Client(server(pipeline_url, TOM)) as c:
        job = await finish(c, (await c.call_tool("request_album", {"albums": [
            {"release_group": f"https://musicbrainz.org/release-group/{rg}"},
            {"release_group": rg2, "preferred_release": rel}]})).data)
        assert job["status"] == "done", job
        assert job["args"] == {"albums": [{"release_group": rg2, "preferred_release": rel},
                                          {"release_group": rg, "preferred_release": None}]}
        assert job["argv"] == [["music-requests", "add", "--", f"{rg2}={rel}", rg]]
        applied = await finish(c, (await c.call_tool("request_album", {"albums": [
            {"release_group": rg2, "preferred_release": rel}, {"release_group": rg}], "apply": True})).data)
        assert applied["status"] == "done" and applied["argv"] == [
            ["music-requests", "add", "--apply", "--", f"{rg2}={rel}", rg]], applied
        for albums in ([{"release_group": "not an mbid"}],
                       [{"release_group": rg, "preferred_release": f"https://musicbrainz.org/release-group/{rg}"}]):
            with pytest.raises(ToolError):
                await c.call_tool("request_album", {"albums": albums})
    async with Client(server(pipeline_url, PILOT)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("request_album", {"albums": [{"release_group": rg}]})
        # No music database here: the read says so.
        listed = (await c.call_tool("list_requests", {})).data
        assert listed["status"] == "unavailable" and listed["error"]


async def test_bad_requests_reach_the_model_as_errors(pipeline_url):
    async with Client(server(pipeline_url, TOM)) as c:
        with pytest.raises(ToolError):
            await c.call_tool("merge_items", {"pairs": [[1, 2], [3, 2]]})
        with pytest.raises(ToolError):
            await c.call_tool("requeue_albums", {"since": "not a time"})


async def test_a_wrong_api_token_is_an_upstream_failure(pipeline_url):
    before = REGISTRY.get_sample_value("music_mcp_upstream_failures_total", {"reason": "unauthorized"}) or 0
    async with Client(server(pipeline_url, PILOT, token="wrong")) as c:
        with pytest.raises(ToolError, match="token was refused"):
            await c.call_tool("library_status", {})
    after = REGISTRY.get_sample_value("music_mcp_upstream_failures_total", {"reason": "unauthorized"})
    assert after == before + 1

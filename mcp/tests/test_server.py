"""The tool surface over a fake pipeline: routes, bodies, tiers and error handling."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from prometheus_client import REGISTRY

from music_mcp import auth as auth_mod
from music_mcp.auth import Caller, caller_resolver
from music_mcp.guide import GUIDE_DIR, Guide
from music_mcp.server import build_server

TOM = Caller("sub-tom", "write")
PILOT = Caller("pilot", "read")

READS = {"library_status", "find_albums", "get_album", "get_item", "list_quarantine", "get_playlist",
         "get_job", "get_guide", "audit_dupes", "audit_lengths", "audit_releases", "audit_want_albums", "audit_titles", "recheck_recordings", "search_releases",
         "album_history", "digest_summary", "list_overrides", "list_requests", "get_later"}
WRITES = {"reset_album", "requeue_albums", "merge_items", "replace_item", "unlink_spotify_id", "embed_covers", "canon_albums",
          "apply_recheck",
          "trigger_scan", "trigger_album_tick", "set_search_override", "clear_search_override", "hold_album",
          "release_album", "pin_track", "unpin_track", "request_album", "remove_from_later"}
# One minimal call per write tool.
WRITE_CALLS = {
    "reset_album": {"query": "abbey road"},
    "requeue_albums": {"ids": ["ALB1"]},
    "merge_items": {"pairs": [[1, 2]]},
    "replace_item": {"id": 1, "youtube_url": "dQw4w9WgXcQ"},
    "unlink_spotify_id": {"id": 1, "track": "68ozhSrI4eLbnUgih1SlOT"},
    "embed_covers": {},
    "canon_albums": {},
    "apply_recheck": {"run_id": "3f2a9c0d1e2b4a5c"},
    "trigger_scan": {},
    "trigger_album_tick": {},
    "set_search_override": {"album": "ALB1", "words": "four tet"},
    "clear_search_override": {"album": "ALB1"},
    "hold_album": {"album": "ALB1", "reason": "curated"},
    "release_album": {"album": "ALB1"},
    "pin_track": {"track": "68ozhSrI4eLbnUgih1SlOT", "url": "https://music.youtube.com/watch?v=29Sh3yEaBP8"},
    "unpin_track": {"track": "68ozhSrI4eLbnUgih1SlOT"},
    "request_album": {"albums": [{"release_group": "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"}]},
    "remove_from_later": {"through": 1},
}
# Never exposed (homelab#2412: irreversible, workstation-only).
IRREVERSIBLE = {"only_copies", "rollback", "force", "allow_manual", "quarantine"}


def failures(reason: str) -> float:
    return REGISTRY.get_sample_value("music_mcp_upstream_failures_total", {"reason": reason}) or 0.0


async def test_tool_surface_is_the_v1_list_and_nothing_irreversible(pipeline):
    async with Client(build_server(pipeline, lambda: TOM)) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == READS | WRITES
    for t in tools:
        params = set((t.inputSchema or {}).get("properties", {}))
        assert not params & IRREVERSIBLE, (t.name, params)
        assert bool((t.annotations and t.annotations.readOnlyHint)) == (t.name in READS), t.name


@pytest.mark.parametrize(("tool", "args", "method", "path", "params"), [
    ("library_status", {}, "GET", "/mcp-api/library_status", {}),
    ("find_albums", {"query": "abbey"}, "GET", "/mcp-api/albums", {"query": "abbey"}),
    ("get_album", {"query": "Bob Dylan — Blood On The Tracks"}, "GET", "/mcp-api/album",
     {"query": "Bob Dylan — Blood On The Tracks"}),
    ("get_item", {"id": 10001}, "GET", "/mcp-api/items/10001", {}),
    ("list_quarantine", {"kind": "rejected", "query": "chill", "offset": 200}, "GET", "/mcp-api/quarantine",
     {"kind": "rejected", "query": "chill", "offset": "200"}),
    ("list_quarantine", {"kind": "replaced"}, "GET", "/mcp-api/quarantine", {"kind": "replaced", "offset": "0"}),
    ("get_playlist", {"name": "chill/../x"}, "GET", "/mcp-api/playlists/chill%2F..%2Fx", {"offset": "0"}),
    ("get_job", {"job_id": "3f2a", "offset": 200}, "GET", "/mcp-api/jobs/3f2a", {"offset": "200"}),
    ("album_history", {"query": "abbey road"}, "GET", "/mcp-api/album_history", {"query": "abbey road", "offset": "0"}),
    ("album_history", {"query": "ALB1", "since": "2026-10-01T00:00:00Z", "offset": 200}, "GET",
     "/mcp-api/album_history", {"query": "ALB1", "since": "2026-10-01T00:00:00Z", "offset": "200"}),
    ("digest_summary", {}, "GET", "/mcp-api/digest_summary", {}),
    ("digest_summary", {"since": "2026-10-28T05:00:00Z"}, "GET", "/mcp-api/digest_summary",
     {"since": "2026-10-28T05:00:00Z"}),
    ("list_overrides", {}, "GET", "/mcp-api/overrides", {"offset": "0"}),
    ("list_overrides", {"kind": "pin", "offset": 200}, "GET", "/mcp-api/overrides", {"kind": "pin", "offset": "200"}),
    ("list_requests", {}, "GET", "/mcp-api/requests", {"offset": "0"}),
    ("list_requests", {"status": "missing", "offset": 200}, "GET", "/mcp-api/requests",
     {"status": "missing", "offset": "200"}),
    ("get_later", {}, "GET", "/mcp-api/later", {"offset": "0"}),
])
async def test_reads_route_to_the_contract(pipeline, fake, tool, args, method, path, params):
    async with Client(build_server(pipeline, lambda: PILOT)) as client:
        await client.call_tool(tool, args)
    assert fake.last.method == method
    assert fake.last.url.raw_path.decode().split("?")[0] == path
    assert dict(fake.last.url.params) == params
    assert fake.last.headers["X-MCP-Caller"] == "pilot"
    assert fake.last.headers["X-MCP-Tier"] == "read"


@pytest.mark.parametrize(("tool", "args", "path", "body"), [
    ("audit_dupes", {}, "/mcp-api/run/audit_dupes", {}),
    ("audit_dupes", {"fingerprint_ids": []}, "/mcp-api/run/audit_dupes", {"fingerprint_ids": []}),
    ("audit_lengths", {"silence": True}, "/mcp-api/run/audit_lengths", {"silence": True}),
    ("audit_releases", {}, "/mcp-api/run/audit_releases", {}),
    ("audit_releases", {"replaced_query": ""}, "/mcp-api/run/audit_releases", {"replaced_query": ""}),
    ("audit_want_albums", {}, "/mcp-api/run/audit_want_albums", {}),
    ("audit_titles", {}, "/mcp-api/run/audit_titles", {}),
    ("recheck_recordings", {}, "/mcp-api/run/recheck_recordings", {}),
    ("recheck_recordings", {"ids": [6994, 5672]}, "/mcp-api/run/recheck_recordings", {"ids": [6994, 5672]}),
    ("recheck_recordings", {"resume": "3f2a9c0d1e2b4a5c"}, "/mcp-api/run/recheck_recordings",
     {"resume": "3f2a9c0d1e2b4a5c"}),
    ("apply_recheck", {"run_id": "3f2a9c0d1e2b4a5c", "apply": True}, "/mcp-api/run/apply_recheck",
     {"run_id": "3f2a9c0d1e2b4a5c", "apply": True}),
    ("search_releases", {"query": "band the album"}, "/mcp-api/run/search_releases",
     {"query": "band the album", "limit": 5}),
    ("reset_album", {"query": "abbey road", "reimport": True}, "/mcp-api/run/reset_album",
     {"query": "abbey road", "reimport": True, "apply": False}),
    ("requeue_albums", {"since": "2026-10-20T00:00:00", "apply": True}, "/mcp-api/run/requeue_albums",
     {"since": "2026-10-20T00:00:00", "apply": True}),
    ("merge_items", {"pairs": [[1, 2], [3, 4]]}, "/mcp-api/run/merge_items",
     {"pairs": [[1, 2], [3, 4]], "apply": False}),
    ("replace_item", {"id": 7, "youtube_url": "https://youtu.be/dQw4w9WgXcQ"}, "/mcp-api/run/replace_item",
     {"id": 7, "youtube_url": "https://youtu.be/dQw4w9WgXcQ", "apply": False}),
    ("unlink_spotify_id", {"id": 9674, "track": "2o49Twc3qrNMOt8gq9W06L"}, "/mcp-api/run/unlink_spotify_id",
     {"id": 9674, "track": "2o49Twc3qrNMOt8gq9W06L", "apply": False}),
    ("embed_covers", {"apply": True}, "/mcp-api/run/embed_covers", {"apply": True}),
    ("canon_albums", {"refresh": True, "mb_budget": 5}, "/mcp-api/run/canon_albums",
     {"refresh": True, "search_budget": None, "mb_budget": 5, "apply": False}),
    ("trigger_scan", {}, "/mcp-api/trigger/scan", {}),
    ("trigger_album_tick", {}, "/mcp-api/trigger/album_tick", {}),
    ("set_search_override", {"album": "ALB1", "words": "four tet"}, "/mcp-api/run/set_search_override",
     {"album": "ALB1", "words": "four tet", "reason": "", "apply": False}),
    ("clear_search_override", {"album": "ALB1", "apply": True}, "/mcp-api/run/clear_search_override",
     {"album": "ALB1", "apply": True}),
    ("hold_album", {"album": "ALB1", "reason": "curated"}, "/mcp-api/run/hold_album",
     {"album": "ALB1", "reason": "curated", "apply": False}),
    ("release_album", {"album": "ALB1"}, "/mcp-api/run/release_album", {"album": "ALB1", "apply": False}),
    ("pin_track", {"track": "68ozhSrI4eLbnUgih1SlOT", "url": "https://music.youtube.com/watch?v=x", "label": "1"},
     "/mcp-api/run/pin_track", {"track": "68ozhSrI4eLbnUgih1SlOT", "url": "https://music.youtube.com/watch?v=x",
                                "label": "1", "reason": "", "apply": False}),
    ("unpin_track", {"track": "68ozhSrI4eLbnUgih1SlOT"}, "/mcp-api/run/unpin_track",
     {"track": "68ozhSrI4eLbnUgih1SlOT", "apply": False}),
    ("request_album", {"albums": [{"release_group": "rg-1"}, {"release_group": "rg-2", "preferred_release": "r-2"}],
                       "apply": True}, "/mcp-api/run/request_album",
     {"albums": [{"release_group": "rg-1", "preferred_release": None},
                 {"release_group": "rg-2", "preferred_release": "r-2"}], "later": False, "apply": True}),
    ("request_album", {"albums": [{"release_group": "rg-1"}], "later": True}, "/mcp-api/run/request_album",
     {"albums": [{"release_group": "rg-1", "preferred_release": None}], "later": True, "apply": False}),
    ("remove_from_later", {"release_groups": ["rg-1"], "through": 2}, "/mcp-api/run/remove_from_later",
     {"release_groups": ["rg-1"], "albums": [], "through": 2, "apply": False}),
    ("remove_from_later", {"through": 1, "apply": True}, "/mcp-api/run/remove_from_later",
     {"release_groups": [], "albums": [], "through": 1, "apply": True}),
    ("remove_from_later", {"albums": ["band — album"]}, "/mcp-api/run/remove_from_later",
     {"release_groups": [], "albums": ["band — album"], "apply": False}),
])
async def test_runs_post_the_contract_body(pipeline, fake, tool, args, path, body):
    async with Client(build_server(pipeline, lambda: TOM)) as client:
        await client.call_tool(tool, args)
    assert fake.last.method == "POST"
    assert fake.last.url.path == path
    assert fake.body() == body
    assert fake.last.headers["X-MCP-Caller"] == "sub-tom"
    assert fake.last.headers["X-MCP-Tier"] == "write"


@pytest.mark.parametrize("tool", sorted(WRITES))
async def test_a_read_caller_is_refused_every_write_before_the_pipeline(pipeline, fake, tool):
    async with Client(build_server(pipeline, lambda: PILOT)) as client:
        with pytest.raises(ToolError, match="read-only"):
            await client.call_tool(tool, WRITE_CALLS[tool])
    assert fake.requests == []


async def test_requeue_needs_ids_or_since(pipeline, fake):
    async with Client(build_server(pipeline, lambda: TOM)) as client:
        with pytest.raises(ToolError, match="ids, since"):
            await client.call_tool("requeue_albums", {})
    assert fake.requests == []


async def test_merge_pairs_must_be_pairs_of_ids(pipeline, fake):
    async with Client(build_server(pipeline, lambda: TOM)) as client:
        for pairs in ([], [[1]], [[1, 2, 3]], [[0, 2]], [["a", "b"]]):
            with pytest.raises(ToolError):
                await client.call_tool("merge_items", {"pairs": pairs})
    assert fake.requests == []


@pytest.mark.parametrize(("code", "body"), [
    (409, {"status": "ambiguous", "query": "abbey", "candidates": [{"key": "A"}, {"key": "B"}]}),
    (404, {"status": "not_found", "query": "zzz"}),
    (409, {"status": "refused", "reason": "no_dry_run", "error": "run reset_album with apply=false first"}),
    (503, {"status": "busy", "retry": True, "error": "beets library is locked"}),
    (503, {"status": "busy", "queue": []}),
    (404, {"status": "unknown", "error": "unknown (pruned or lost to a restart)"}),
    (501, {"status": "not_implemented", "error": "waits for homelab#2411"}),
    (202, {"job_id": "j1", "status": "running"}),
    (200, {"status": "already_pending", "flow": "music-scan", "state": "RUNNING"}),
])
async def test_contract_statuses_reach_the_model_as_data(pipeline, fake, code, body):
    fake.handler = lambda r: httpx.Response(code, json=body)
    before = sum(failures(r) for r in ("server_error", "connect", "timeout", "unauthorized"))
    async with Client(build_server(pipeline, lambda: TOM)) as client:
        result = await client.call_tool("get_album", {"query": "abbey"})
    assert result.data == body
    assert sum(failures(r) for r in ("server_error", "connect", "timeout", "unauthorized")) == before


@pytest.mark.parametrize(("code", "body", "match"), [
    (400, {"error": "merge_items: an ID appears twice"}, "appears twice"),
    (403, {"error": "write tier required"}, "write tier required"),
])
async def test_bad_requests_become_tool_errors(pipeline, fake, code, body, match):
    fake.handler = lambda r: httpx.Response(code, json=body)
    async with Client(build_server(pipeline, lambda: TOM)) as client:
        with pytest.raises(ToolError, match=match):
            await client.call_tool("merge_items", {"pairs": [[1, 2]]})


@pytest.mark.parametrize(("handler", "reason", "match"), [
    (lambda r: httpx.Response(401, json={"error": "unauthorized"}), "unauthorized", "token was refused"),
    (lambda r: httpx.Response(500, text="Internal Server Error"), "server_error", "HTTP 500"),
    (lambda r: httpx.Response(502, text="bad gateway"), "server_error", "HTTP 502"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "connect", "unreachable"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "timeout", "in time"),
])
async def test_upstream_failures_are_counted(pipeline, fake, handler, reason, match):
    fake.handler = handler
    before = failures(reason)
    async with Client(build_server(pipeline, lambda: PILOT)) as client:
        with pytest.raises(ToolError, match=match):
            await client.call_tool("library_status", {})
    assert failures(reason) == before + 1


async def test_path_segments_cannot_be_dot_segments(pipeline, fake):
    async with Client(build_server(pipeline, lambda: PILOT)) as client:
        for name in ("..", "."):
            with pytest.raises(ToolError, match="not a valid name"):
                await client.call_tool("get_playlist", {"name": name})
    assert fake.requests == []


async def test_guide(pipeline):
    async with Client(build_server(pipeline, lambda: PILOT)) as client:
        index = (await client.call_tool("get_guide", {})).data
        assert index["topic"] == "index"
        assert {"classify", "album-mode", "workflow", "verify"} <= set(index["topics"])
        for topic in index["topics"]:
            assert f"`{topic}`" in index["markdown"] or topic == "index", f"index doesn't list {topic}"
        classify = (await client.call_tool("get_guide", {"topic": "Classify.md"})).data
        assert classify["status"] == "ok" and "merge_items" in classify["markdown"]
        missing = (await client.call_tool("get_guide", {"topic": "../server"})).data
        assert missing["status"] == "not_found"


def test_guide_names_only_real_tools():
    import re

    known = READS | WRITES
    for topic, text in ((t, Guide().get(t)["markdown"]) for t in Guide().topics):
        for name in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text):
            if name.startswith(("music_", "replaced_", "fingerprint_", "item_", "next_", "job_", "song_",
                                "search_", "mb_", "musicbrainz_", "spotify_", "merged_", "waiting_", "already_")):
                continue
            assert name in known, f"guide/{topic}.md names `{name}`, not a tool"
    assert (GUIDE_DIR / "index.md").exists()


def test_guide_says_no_kubectl_except_for_workstation_operations():
    for topic in Guide().topics:
        text = Guide().get(topic)["markdown"]
        if topic != "workflow":
            assert "kubectl" not in text, topic


def _token(monkeypatch, claims, token="jwt"):
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: SimpleNamespace(claims=claims, token=token))


def test_oidc_group_gate_and_tier(monkeypatch):
    resolve = caller_resolver("music", None)
    _token(monkeypatch, {"sub": "abc", "groups": ["admins"]})
    with pytest.raises(ToolError, match="not in the 'music' group"):
        resolve()
    _token(monkeypatch, {"sub": "abc", "groups": ["music"]})
    assert resolve() == Caller("abc", "write")
    _token(monkeypatch, {"groups": ["music"]})
    with pytest.raises(ToolError, match="no user id"):
        resolve()
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: None)
    with pytest.raises(ToolError, match="not signed in"):
        resolve()


def test_a_pilot_sub_claim_alone_is_not_the_static_token(monkeypatch):
    # Without the static token's hash, "pilot" is just a sub: it still needs the group.
    resolve = caller_resolver("music", None)
    _token(monkeypatch, {"sub": "pilot"})
    with pytest.raises(ToolError, match="group"):
        resolve()


async def test_a_refused_caller_cannot_read_either(pipeline, fake):
    def outsider():
        raise ToolError("your account is not in the 'music' group")

    async with Client(build_server(pipeline, outsider)) as client:
        with pytest.raises(ToolError, match="music"):
            await client.call_tool("library_status", {})
    assert fake.requests == []

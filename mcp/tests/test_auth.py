"""The real OIDCProxy and static-token wiring, over HTTP against the built app."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from fastmcp.server.auth.redirect_validation import matches_allowed_pattern, validate_redirect_uri
from key_value.aio.stores.memory import MemoryStore

from music_mcp.auth import build_auth, caller_resolver
from music_mcp.server import build_server
from tests.conftest import ALLOWED, BASE, CLAUDE, READ_TOKEN, ROUTE_HEADER

# FastMCP (fastmcp.server.auth.redirect_validation, 4.1.4) parses the URI and each pattern
# and compares scheme, host and port separately, rejecting userinfo and dot-segments; a
# string glob would let these through `http://localhost:*`.
REJECTED = [
    "http://localhost.evil.com/callback",
    "http://localhost.evil.com:8080/callback",
    "http://localhost@evil.com/callback",
    "http://localhost:8080@evil.com/callback",
    "http://user:pw@localhost:8080/callback",
    "http://evil.com/?r=http://localhost:8080",
    "http://127.0.0.1:8080/callback",  # loopback, but not the host the pattern names
    "https://localhost:8080/callback",  # scheme is part of the pattern
    "http://localhost:8080/a/../../steal",
    "https://claude.ai.evil.com/api/mcp/auth_callback",
    "https://evil.com/api/mcp/auth_callback",
    "https://claude.ai/api/mcp/other",
    "https://claude.ai:8443/api/mcp/auth_callback",
    "http://claude.ai/api/mcp/auth_callback",
    "javascript://localhost:80/%0aalert(1)",
]
ACCEPTED = [
    CLAUDE,
    "http://localhost:33418/callback",
    "http://localhost/callback",
    "http://LOCALHOST:5173/",
]


@pytest.mark.parametrize("uri", REJECTED)
def test_redirect_matching_rejects_lookalikes(uri):
    assert not validate_redirect_uri(uri, ALLOWED)
    assert not any(matches_allowed_pattern(uri, p) for p in ALLOWED)


@pytest.mark.parametrize("uri", ACCEPTED)
def test_redirect_matching_accepts_the_configured_ones(uri):
    assert validate_redirect_uri(uri, ALLOWED)


@asynccontextmanager
async def served(settings, pipeline, **overrides):
    """The app over ASGI. A context manager in the test's own task, not a fixture: the app's
    lifespan can't be exited from a fixture's teardown task."""
    settings = replace(settings, **overrides)
    kv = MemoryStore()
    auth, static = build_auth(settings, kv)
    asgi = build_server(pipeline, caller_resolver(settings.required_group, static), auth=auth).http_app()
    async with asgi.router.lifespan_context(asgi):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url=BASE) as c:
            yield c, kv, auth


REG = {"client_name": "x", "token_endpoint_auth_method": "none",
       "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}


async def test_dcr_admits_only_the_allowed_redirects_and_stores_encrypted(settings, pipeline):
    async with served(settings, pipeline) as (c, kv, _):
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert "groups" in meta["scopes_supported"]
        assert (await c.get("/healthz")).text == "ok"
        for bad in ["http://localhost.evil.com/cb", "http://localhost@evil.com/cb", "https://evil.example/cb"]:
            resp = await c.post("/register", json=REG | {"redirect_uris": [bad]})
            assert resp.status_code == 400, (bad, resp.text)
        for good in [CLAUDE, "http://localhost:33418/callback"]:
            resp = await c.post("/register", json=REG | {"redirect_uris": [good]})
            assert resp.status_code == 201, (good, resp.text)

    # What the store holds, as the database would (MemoryStore keeps each entry's JSON).
    stored = [entry.json_str for coll in kv._cache.values() for entry in coll._cache.values()]
    assert stored, "client registrations not persisted"
    assert not any("localhost:33418" in s or CLAUDE in s for s in stored), "stored in plaintext"


async def test_pocket_id_does_not_get_the_resource(settings, pipeline):
    async with served(settings, pipeline) as (_, _, auth):
        # Pocket ID rejects a resource that isn't one of its APIs (a private FastMCP method:
        # the consent page sits between /authorize and the redirect).
        upstream = auth.server._build_upstream_authorize_url(
            "txn", {"resource": f"{BASE}/mcp", "scopes": ["openid", "groups"]})
    query = httpx.URL(upstream).params
    assert "resource" not in query
    assert query["scope"] == "openid groups"


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
STATIC = {"Authorization": f"Bearer {READ_TOKEN}"}


def _rpc_result(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


async def _session(c: httpx.AsyncClient, headers: dict) -> dict:
    resp = await c.post("/mcp", json=INIT, headers=ACCEPT | headers)
    assert resp.status_code == 200, resp.text
    session = {"mcp-session-id": resp.headers["mcp-session-id"]} | ACCEPT | headers
    await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
    return session


async def _call(c: httpx.AsyncClient, session: dict, tool: str, args: dict) -> dict:
    resp = await c.post("/mcp", headers=session, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    assert resp.status_code == 200, resp.text
    return _rpc_result(resp)["result"]


@pytest.mark.parametrize("headers", [
    {},
    {ROUTE_HEADER: "public"},
    {ROUTE_HEADER: "Private"},
    {ROUTE_HEADER: "private "},
    {ROUTE_HEADER: "private,private"},
    {"X-Other-Route": "private"},
])
async def test_static_token_refused_off_the_private_route(settings, pipeline, headers):
    async with served(settings, pipeline) as (c, _, _):
        resp = await c.post("/mcp", json=INIT, headers=ACCEPT | headers | STATIC)
    assert resp.status_code == 401


async def test_static_token_refused_with_a_duplicated_route_header(settings, pipeline):
    headers = [*STATIC.items(), (ROUTE_HEADER, "public"), (ROUTE_HEADER, "private"), *ACCEPT.items()]
    async with served(settings, pipeline) as (c, _, _):
        resp = await c.post("/mcp", json=INIT, headers=headers)
    assert resp.status_code == 401


async def test_a_wrong_token_is_refused_on_the_private_route(settings, pipeline):
    async with served(settings, pipeline) as (c, _, _):
        for token in [READ_TOKEN + "x", READ_TOKEN.upper(), "a" * 64, settings.read_token_sha256]:
            resp = await c.post("/mcp", json=INIT,
                                headers=ACCEPT | {ROUTE_HEADER: "private", "Authorization": f"Bearer {token}"})
            assert resp.status_code == 401, token


async def test_static_token_reads_as_pilot_and_is_refused_writes(settings, pipeline, fake):
    async with served(settings, pipeline) as (c, _, _):
        session = await _session(c, {ROUTE_HEADER: "private"} | STATIC)

        result = await _call(c, session, "library_status", {})
        assert not result.get("isError"), result
        assert fake.last.headers["X-MCP-Caller"] == "pilot"
        assert fake.last.headers["X-MCP-Tier"] == "read"
        assert fake.last.headers["Authorization"] == "Bearer mcp-api-token"

        seen = len(fake.requests)
        for tool, args in [("reset_album", {"query": "abbey road"}), ("trigger_scan", {}),
                           ("merge_items", {"pairs": [[1, 2]], "apply": True})]:
            result = await _call(c, session, tool, args)
            assert result.get("isError"), tool
            assert "read-only" in result["content"][0]["text"]
        assert len(fake.requests) == seen, "a refused write reached the pipeline"


async def test_no_static_token_when_unconfigured(settings, pipeline):
    async with served(settings, pipeline, read_token_sha256=None) as (c, _, auth):
        resp = await c.post("/mcp", json=INIT, headers=ACCEPT | {ROUTE_HEADER: "private"} | STATIC)
    assert resp.status_code == 401
    assert type(auth).__name__ == "OIDCProxy"

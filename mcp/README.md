# music-mcp

The Music MCP ([homelab#2412](https://github.com/sometimeskind/homelab/issues/2412)): a FastMCP
server that diagnoses the music library, and fixes it recoverably, from a claude.ai connector
and from Claude Code. It runs as its own pod (`ghcr.io/sometimeskind/music-mcp`), never in the
pipeline pod, and reaches only three things: the pipeline's `/mcp-api/*` (contract:
[`service/MCP_API.md`](../service/MCP_API.md)), Pocket ID, and its own Postgres (the OAuth
proxy's store). No tool can return a credential, because the adapter holds none of the
pipeline's.

## Auth and tiers

| Caller | How | Tier |
|---|---|---|
| claude.ai (and Code during the private phase) | FastMCP `OIDCProxy` in front of Pocket ID, checked for the `MUSIC_MCP_REQUIRED_GROUP` group on every tool call | read + write |
| Claude Code on pilot | a static bearer token whose SHA-256 matches `MUSIC_MCP_READ_TOKEN_SHA256`, accepted only when the `MUSIC_MCP_ROUTE_HEADER` header is exactly `private` (set by the private HTTPRoute) | read |

The adapter sends `X-MCP-Caller` (OIDC `sub`, or `pilot`) and `X-MCP-Tier` to the pipeline. It
refuses a read-tier write itself, and the pipeline refuses it again (403). The auth is copied
from meals-mcp: the py-key-value Postgres store pinned exactly and Fernet-wrapped, and
`forward_resource=False`.

**Redirect URIs** in `MUSIC_MCP_ALLOWED_REDIRECTS` are matched by FastMCP
(`fastmcp/server/auth/redirect_validation.py`, 4.1.4) on the parsed URI. Scheme and host must
be equal; the port is equal or `*`; the path is an fnmatch glob, and an empty pattern path
matches any path. A URI with userinfo or dot-segments is rejected. So
`http://localhost:*` admits any port on `localhost` only. `http://localhost.evil.com/…` and
`http://localhost@evil.com/…` fail, as `tests/test_auth.py` checks.

## Configuration

Settings come from env (`music_mcp/config.py`). The deployment contract is homelab#2417:
`MUSIC_MCP_BASE_URL`, `_OIDC_CONFIG_URL`, `_OIDC_CLIENT_ID`, `_OIDC_CLIENT_SECRET`,
`_REQUIRED_GROUP` (default `music`), `_ALLOWED_REDIRECTS`, `_DATABASE_URL`, `_JWT_SIGNING_KEY`,
`_STORAGE_KEY` (a Fernet key), `_PIPELINE_URL`, `_API_TOKEN` (the pipeline's `MCP_API_TOKEN`),
`_READ_TOKEN_SHA256` (unset: no static token) and `_ROUTE_HEADER`.

The server listens on port 8000 (`/mcp`, the OAuth routes, `/healthz`) and serves metrics on
9090: `music_mcp_upstream_failures_total{reason}` (alert `MusicMcpPipelineUnreachable`),
`music_mcp_tool_calls_total{tool,outcome}` and `music_mcp_static_token_refused_total`.

## The guide

`guide/*.md` is the only copy of the library's domain knowledge: the symptom table, album mode,
the dry-run workflow and the verify timings. `get_guide(topic)` serves it, and the Claude Code
`music-library` skill reads the same files. Edit it here.

## Tests

`just test-mcp` runs the unit tests and the contract test. The adapter runs in its own venv,
and the real Flask app (`service/tests/mcp_harness.py`) runs as a subprocess of the service's
Python, so fastmcp and prefect never share an environment. The harness keeps its state in a
database it creates on `MUSIC_TEST_DATABASE_URL`'s Postgres (`just test-mcp` starts a throwaway
one). CI runs the same `mcp/test.sh`, against the job's postgres service.

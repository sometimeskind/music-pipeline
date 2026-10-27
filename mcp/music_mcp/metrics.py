"""Prometheus metrics, served on their own port so /metrics never sits behind the edge."""

from prometheus_client import Counter

TOOL_CALLS = Counter("music_mcp_tool_calls_total", "MCP tool calls", ["tool", "outcome"])
# MusicMcpPipelineUnreachable (homelab) alerts on this: connection errors, timeouts, 5xx that
# aren't a reported busy state, and 401s from a mismatched MCP_API_TOKEN.
UPSTREAM_FAILURES = Counter(
    "music_mcp_upstream_failures_total", "Failed calls from the adapter to the pipeline's /mcp-api/*", ["reason"]
)
STATIC_TOKEN_REFUSED = Counter(
    "music_mcp_static_token_refused_total", "Pilot's static token presented off the private route"
)

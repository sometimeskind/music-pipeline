"""Settings, read from MUSIC_MCP_* environment variables (the contract with homelab#2417)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"{name} is required")
    return value


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class ServerSettings:
    base_url: str
    oidc_config_url: str
    oidc_client_id: str
    oidc_client_secret: str
    database_url: str
    jwt_signing_key: str
    storage_key: str
    pipeline_url: str
    api_token: str
    required_group: str = "music"
    allowed_redirects: list[str] = field(default_factory=list)
    # Pilot's static read token: only its SHA-256 is configured, and it is accepted only when
    # the route header says "private". Unset, the static token is off.
    read_token_sha256: str | None = None
    route_header: str | None = None
    # "" binds every address of both families. The cluster is dual-stack and the gateway may
    # pick the IPv6 endpoint; uvicorn makes "::" IPv6-only.
    host: str = ""
    port: int = 8000
    metrics_port: int = 9090

    def __post_init__(self) -> None:
        if self.read_token_sha256 is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", self.read_token_sha256):
                raise RuntimeError("MUSIC_MCP_READ_TOKEN_SHA256 must be 64 lowercase hex characters")
            if not self.route_header:
                raise RuntimeError("MUSIC_MCP_READ_TOKEN_SHA256 needs MUSIC_MCP_ROUTE_HEADER")

    @classmethod
    def from_env(cls) -> ServerSettings:
        sha = (os.environ.get("MUSIC_MCP_READ_TOKEN_SHA256") or "").strip().lower()
        return cls(
            base_url=_env("MUSIC_MCP_BASE_URL"),
            oidc_config_url=_env("MUSIC_MCP_OIDC_CONFIG_URL"),
            oidc_client_id=_env("MUSIC_MCP_OIDC_CLIENT_ID"),
            oidc_client_secret=_env("MUSIC_MCP_OIDC_CLIENT_SECRET"),
            database_url=_env("MUSIC_MCP_DATABASE_URL"),
            jwt_signing_key=_env("MUSIC_MCP_JWT_SIGNING_KEY"),
            storage_key=_env("MUSIC_MCP_STORAGE_KEY"),
            pipeline_url=_env("MUSIC_MCP_PIPELINE_URL").rstrip("/"),
            api_token=_env("MUSIC_MCP_API_TOKEN"),
            required_group=_env("MUSIC_MCP_REQUIRED_GROUP", "music"),
            allowed_redirects=[
                u.strip()
                for u in _env("MUSIC_MCP_ALLOWED_REDIRECTS", "https://claude.ai/api/mcp/auth_callback").split(",")
                if u.strip()
            ],
            read_token_sha256=sha or None,
            route_header=(os.environ.get("MUSIC_MCP_ROUTE_HEADER") or "").strip() or None,
            port=_int("MUSIC_MCP_PORT", 8000),
            metrics_port=_int("MUSIC_MCP_METRICS_PORT", 9090),
        )

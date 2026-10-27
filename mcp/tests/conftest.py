"""Fixtures: a stub Pocket ID discovery document, settings, and a fake pipeline."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from collections.abc import Callable

import httpx
import pytest
from cryptography.fernet import Fernet

from music_mcp.config import ServerSettings
from music_mcp.pipeline import Pipeline

CLAUDE = "https://claude.ai/api/mcp/auth_callback"
# What homelab's private phase sets (kubernetes/music-mcp/music-mcp.yaml, homelab#2417).
ALLOWED = [CLAUDE, "http://localhost:*"]
READ_TOKEN = "pilot-read-token-for-tests"
ROUTE_HEADER = "X-Music-Mcp-Route"
BASE = "https://music.example.test"


@pytest.fixture
def issuer():
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            base = holder["url"]
            docs = {
                "/.well-known/openid-configuration": {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/api/oidc/token",
                    "jwks_uri": f"{base}/.well-known/jwks.json",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
                "/.well-known/jwks.json": {"keys": []},
            }
            body = json.dumps(docs.get(self.path, {})).encode()
            self.send_response(200 if self.path in docs else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    holder["url"] = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield holder["url"]
    srv.shutdown()


@pytest.fixture
def settings(issuer) -> ServerSettings:
    return ServerSettings(
        base_url=BASE,
        oidc_config_url=f"{issuer}/.well-known/openid-configuration",
        oidc_client_id="music-mcp",
        oidc_client_secret="s3cret",
        database_url="unused",
        jwt_signing_key="jwt-key",
        storage_key=Fernet.generate_key().decode(),
        pipeline_url="http://pipeline.test:8080",
        api_token="mcp-api-token",
        required_group="music",
        allowed_redirects=ALLOWED,
        read_token_sha256=hashlib.sha256(READ_TOKEN.encode()).hexdigest(),
        route_header=ROUTE_HEADER,
    )


class FakePipeline:
    """Records each request and answers from a handler (default: 200 {"status": "ok"})."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(200, json={"status": "ok"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, request: httpx.Request | None = None) -> dict:
        return json.loads((request or self.last).content or b"{}")


@pytest.fixture
def fake() -> FakePipeline:
    return FakePipeline()


@pytest.fixture
async def pipeline(fake):
    p = Pipeline("http://pipeline.test:8080", "mcp-api-token", transport=httpx.MockTransport(fake))
    yield p
    await p.aclose()

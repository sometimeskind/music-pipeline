"""Who is calling, and at which tier.

Two ways in:

- **OIDC** (claude.ai, and Code during the private phase): FastMCP's OIDCProxy in front of
  Pocket ID, as meals-mcp does. A user in the required group gets the write tier.
- **Pilot's static token** (Claude Code sessions): accepted only when its SHA-256 matches
  MUSIC_MCP_READ_TOKEN_SHA256 *and* the route header (set by the private HTTPRoute,
  overwriting the client's) is exactly `private`. Always the read tier.

Both are checked again on every tool call, not only at the HTTP layer.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request

from music_mcp import metrics

log = logging.getLogger(__name__)

Tier = Literal["read", "write"]
PILOT = "pilot"


@dataclass(frozen=True)
class Caller:
    id: str
    tier: Tier


class StaticReadToken(TokenVerifier):
    """Pilot's read-only bearer token, on the private route only."""

    def __init__(self, sha256_hex: str, route_header: str, required_scopes: list[str] | None = None) -> None:
        super().__init__(required_scopes=required_scopes)
        self._sha256 = sha256_hex.lower()
        self._route_header = route_header

    def _hash_matches(self, token: str) -> bool:
        digest = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(digest, self._sha256)

    def _on_private_route(self) -> bool:
        try:
            request = get_http_request()
        except RuntimeError:
            return False
        # Exactly one value, exactly "private": the private route `set`s it; the public one
        # sets `public`. A client-supplied duplicate never passes.
        return request.headers.getlist(self._route_header) == ["private"]

    def accepts(self, token: str) -> bool:
        if not self._hash_matches(token):
            return False
        if not self._on_private_route():
            metrics.STATIC_TOKEN_REFUSED.inc()
            log.warning("static read token presented without %s: private; refused", self._route_header)
            return False
        return True

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self.accepts(token):
            return None
        return AccessToken(
            token=token,
            client_id=PILOT,
            # The HTTP layer enforces the server's required scopes on every token.
            scopes=list(self.required_scopes),
            expires_at=None,
            claims={"sub": PILOT},
        )


def postgres_store(pool: Any) -> Any:
    """The OAuth proxy's store: a table in the adapter's own CNPG database."""
    from key_value.aio.stores.postgresql import PostgreSQLStore

    return PostgreSQLStore(pool=pool, table_name="oauth_proxy_kv")


def build_auth(settings: Any, key_value: Any) -> tuple[Any, StaticReadToken | None]:
    """OIDCProxy in front of Pocket ID, its state Fernet-encrypted in `key_value`
    (postgres_store in production), plus pilot's static token when configured.
    Returns (auth, static verifier)."""
    from cryptography.fernet import Fernet
    from fastmcp.server.auth import MultiAuth
    from fastmcp.server.auth.oidc_proxy import OIDCProxy
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    proxy = OIDCProxy(
        config_url=settings.oidc_config_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=settings.base_url,
        # Pocket ID's id_token carries the groups claim the tools check.
        verify_id_token=True,
        required_scopes=["openid", "profile", "email", "groups"],
        # Matched by parsed scheme, host and port (fastmcp.server.auth.redirect_validation),
        # never as a string glob: see tests/test_auth.py.
        allowed_client_redirect_uris=settings.allowed_redirects,
        # The client's `resource` names this server, whose tokens the proxy issues itself.
        # Pocket ID only logs the user in, and rejects a resource that isn't one of its APIs.
        forward_resource=False,
        jwt_signing_key=settings.jwt_signing_key,
        client_storage=FernetEncryptionWrapper(
            key_value=key_value,
            fernet=Fernet(settings.storage_key),
            # A rotated key reads old rows as missing: every user reconnects, nothing crashes.
            raise_on_decryption_error=False,
        ),
    )
    if not settings.read_token_sha256:
        return proxy, None
    static = StaticReadToken(settings.read_token_sha256, settings.route_header, proxy.required_scopes)
    return MultiAuth(server=proxy, verifiers={"pilot-static": static}), static


def caller_resolver(required_group: str, static: StaticReadToken | None):
    """The per-call identity check every tool runs before touching the pipeline."""

    def resolve() -> Caller:
        token = get_access_token()
        if token is None:
            raise ToolError("not signed in")
        if static is not None and static.accepts(token.token):
            return Caller(PILOT, "read")
        claims = token.claims or {}
        if required_group not in (claims.get("groups") or []):
            raise ToolError(f"your account is not in the {required_group!r} group")
        sub = claims.get("sub")
        if not sub:
            raise ToolError("the sign-in carries no user id")
        return Caller(str(sub), "write")

    return resolve

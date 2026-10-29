"""Credentials out of what the pipeline shows (#343).

A Prowlarr download link carries Prowlarr's API key as ``apikey=``, and the
indexer link it redirects to carries the indexer's; ``requests`` puts the URL
in its exception messages.  :func:`redact_text` masks credential query
parameters (raw or percent-encoded inside another parameter) and URL user
info; :func:`redact` walks JSON data, also dropping ``download_url`` fields.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "REDACTED"
DROPPED = frozenset({"download_url", "downloadUrl"})
_NAMES = "|".join(map(re.escape, ("apikey", "api_key", "api-key", "key", "token", "access_token", "auth",
                                   "password", "passwd", "pass", "secret", "signature", "sig", "r")))
_PARAM = re.compile(rf"([?&;](?:{_NAMES})=)[^&#\s\"'<>]+", re.IGNORECASE)
_ENCODED = re.compile(rf"((?:%3F|%26)(?:{_NAMES})%3D)(?:(?!%26)[^&#\s\"'<>])+", re.IGNORECASE)
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@\"'<>]+@", re.IGNORECASE)


def redact_text(text: str) -> str:
    """*text* with every credential in a URL masked."""
    text = _PARAM.sub(rf"\g<1>{REDACTED}", text)
    text = _ENCODED.sub(rf"\g<1>{REDACTED}", text)
    return _USERINFO.sub(rf"\g<1>{REDACTED}@", text)


def redact(value: Any) -> tuple[Any, int]:
    """JSON *value* without credentials, and how many fields or strings it changed."""
    changed = 0

    def walk(v: Any) -> Any:
        nonlocal changed
        if isinstance(v, str):
            clean = redact_text(v)
            changed += clean != v
            return clean
        if isinstance(v, dict):
            changed += sum(k in DROPPED for k in v)
            return {k: walk(x) for k, x in v.items() if k not in DROPPED}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value), changed

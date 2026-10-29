"""Credentials out of what the pipeline shows (#343).

A Prowlarr download link carries Prowlarr's API key as ``apikey=``, and the
indexer link it redirects to carries the indexer's; ``requests`` puts the URL
in its exception messages.  :func:`redact_text` masks credential query
parameters (raw or percent-encoded inside another parameter) and URL user
info, and any value of a credential in the environment (``PROWLARR_API_KEY``…);
:func:`redact` walks JSON data, also dropping ``download_url`` fields.
"""

from __future__ import annotations

import os
import re
from typing import Any

REDACTED = "REDACTED"
DROPPED = frozenset({"download_url", "downloadUrl"})
# Any parameter naming a credential (apikey, X-Api-Key, access_token, passwd…), and the r= of NZB links.
_NAME = r"(?:[\w.-]*(?:key|token|pass|secret|auth|sig)[\w.-]*|r)"
_PARAM = re.compile(rf"([?&;]{_NAME}=)[^&#\s\"'<>]+", re.IGNORECASE)
# Percent-encoded inside another parameter, once or more (%3F, %253F…).
_ENCODED = re.compile(rf"(%(?:25)*(?:3F|26){_NAME}%(?:25)*3D)(?:(?!%(?:25)*26)[^&#\s\"'<>])+", re.IGNORECASE)
# User info runs to the last '@' before the host: a password may hold one.
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s\"'<>]+@", re.IGNORECASE)
# The credentials this process holds, masked wherever they appear, in whatever shape.
_SECRET_ENV = re.compile(r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD)$")
_MIN_SECRET = 8


def secrets() -> list[str]:
    """The values of this process's credential variables (``*_API_KEY``, ``*_TOKEN``…), longest first."""
    return sorted({v for k, v in os.environ.items() if _SECRET_ENV.search(k) and len(v) >= _MIN_SECRET},
                  key=len, reverse=True)


def redact_text(text: str, known: list[str] | None = None) -> str:
    """*text* with every credential in a URL, and every one of *known* (default :func:`secrets`), masked."""
    for secret in secrets() if known is None else known:
        text = text.replace(secret, REDACTED)
    text = _PARAM.sub(rf"\g<1>{REDACTED}", text)
    text = _ENCODED.sub(rf"\g<1>{REDACTED}", text)
    return _USERINFO.sub(rf"\g<1>{REDACTED}@", text)


def redact(value: Any) -> tuple[Any, int]:
    """JSON *value* without credentials, and how many fields or strings it changed."""
    changed = 0
    known = secrets()

    def walk(v: Any) -> Any:
        nonlocal changed
        if isinstance(v, str):
            clean = redact_text(v, known)
            changed += clean != v
            return clean
        if isinstance(v, dict):
            changed += sum(k in DROPPED for k in v)
            return {k: walk(x) for k, x in v.items() if k not in DROPPED}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value), changed

"""Fail fast on a long Spotify rate limit instead of sleeping through it (#195).

spotipy retries a 429 through urllib3's ``Retry``, which sleeps for the whole
``Retry-After`` with no cap (62939s, about 17.5h, on 2026-10-04).  The flow then
holds its lock for hours, later runs skip as busy, and no failure alert fires.

:func:`install` mounts an adapter on the shared spotipy session that:

- waits out a ``Retry-After`` of up to :data:`MAX_RETRY_AFTER_SECONDS` as before;
- on a longer one, records the limit's end (process-wide and in :data:`STATE_FILE`),
  pushes it as ``music_spotify_rate_limited_until_seconds`` and raises
  :class:`SpotifyRateLimited`;
- refuses every later request while a recorded limit is active, so a run started
  during a limit fails without calling Spotify, and a spotdl path that swallows the
  first exception (per-track ``reinit_song`` in the downloader) can't call again.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from music_fetch.metrics import _gauge, _push

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 120
# On pipeline-state, next to .albums.json, so it outlives the flow-run process.
STATE_FILE = Path("/root/Music/inbox/spotdl/.spotify-rate-limit.json")

_limit: tuple[datetime, int] | None = None  # (until, retry_after) for this process


class SpotifyRateLimited(RuntimeError):
    """Spotify answered 429 with a Retry-After too long to wait out."""

    def __init__(self, until: datetime, retry_after: int, recorded: bool = False) -> None:
        self.until = until
        self.retry_after = retry_after
        msg = f"Spotify rate-limited: Retry-After {retry_after}s, until {until:%Y-%m-%d %H:%M:%S} UTC"
        if recorded:
            msg += " (recorded earlier; not calling Spotify)"
        super().__init__(msg)


def _load(state_file: Path) -> tuple[datetime, int] | None:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["until"]), int(data["retry_after"])
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        logger.warning("Ignoring unreadable Spotify rate-limit state %s", state_file)
        return None


def check() -> None:
    """Raise :class:`SpotifyRateLimited` while a recorded limit is active."""
    if _limit is not None and _limit[0] > datetime.now(timezone.utc):
        raise SpotifyRateLimited(*_limit, recorded=True)


def trip(retry_after: float) -> SpotifyRateLimited:
    """Record a long rate limit and return the exception to raise."""
    global _limit  # noqa: PLW0603
    state_file = STATE_FILE
    seconds = int(retry_after)
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0)
    _limit = (until, seconds)
    try:
        state_file.write_text(
            json.dumps({"until": until.isoformat(), "retry_after": seconds}), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not record the Spotify rate limit in %s: %s", state_file, exc)
    _push(_gauge("music_spotify_rate_limited_until_seconds", int(until.timestamp())), "music_spotify")
    exc = SpotifyRateLimited(until, seconds)
    logger.error("%s", exc)
    return exc


class FailFastRetry(Retry):
    """urllib3 Retry that raises instead of sleeping through a long Retry-After.

    No extra attributes: ``increment()`` rebuilds the object from Retry's own params.
    """

    @classmethod
    def from_retry(cls, retry: Retry) -> "FailFastRetry":
        """A copy of *retry* (spotipy's settings) with this class's sleep."""
        new = retry.new()
        new.__class__ = cls
        # urllib3 >= 2.6 caps Retry-After at 6h; read the real value so the log
        # line and the gauge say when the limit actually ends.
        new.retry_after_max = 10**9
        return new

    def sleep(self, response=None) -> None:
        if response is not None and self.respect_retry_after_header:
            retry_after = self.get_retry_after(response)
            if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
                raise trip(retry_after)
        super().sleep(response)


class FailFastAdapter(HTTPAdapter):
    """Refuses requests while a recorded rate limit is active."""

    def send(self, request, *args, **kwargs):
        check()
        return super().send(request, *args, **kwargs)


def install(client) -> None:
    """Mount the fail-fast adapter on a spotipy client's session.

    Loads a limit recorded by an earlier run.  Relies on spotipy's private
    ``_session``; ``test_spotify_limit`` checks it still exists, so a spotipy
    bump that drops it fails CI rather than silently restoring the long sleep.
    """
    global _limit  # noqa: PLW0603
    recorded = _load(STATE_FILE)
    if recorded is not None and (_limit is None or recorded[0] > _limit[0]):
        _limit = recorded

    session = client._session
    old = session.get_adapter("https://api.spotify.com/")
    if isinstance(old, FailFastAdapter):
        return
    adapter = FailFastAdapter(max_retries=FailFastRetry.from_retry(old.max_retries))
    session.mount("https://", adapter)
    session.mount("http://", adapter)

"""Tests for music_fetch.spotify_limit — fail fast on a long Spotify 429 (#195).

The 429s come from a local HTTP server, so the real spotipy → requests → urllib3
Retry path runs (``responses`` would mock out the adapter and its retries).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest
import spotipy

import music_fetch.spotify_limit as spotify_limit
from music_fetch.spotify_limit import FailFastAdapter, SpotifyRateLimited


@pytest.fixture(autouse=True)
def _limit_state(tmp_path: Path):
    with mock.patch.object(spotify_limit, "STATE_FILE", tmp_path / ".spotify-rate-limit.json"), \
         mock.patch.object(spotify_limit, "_limit", None), \
         mock.patch.object(spotify_limit, "_push") as push:
        yield push


@pytest.fixture
def server():
    """A fake Spotify API: answers each request with the next queued (status, headers)."""
    queue: list[tuple[int, dict]] = []
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            status, headers = queue.pop(0) if queue else (200, {})
            body = json.dumps({"snapshot_id": "s1"} if status == 200 else {"error": {"status": status}}).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, queue, hits
    httpd.shutdown()


def _client(httpd) -> spotipy.Spotify:
    """A spotipy client configured the way spotdl's official client is."""
    client = spotipy.Spotify(auth="token", status_forcelist=(429, 500, 502, 503, 504, 404))
    client.prefix = f"http://127.0.0.1:{httpd.server_address[1]}/v1/"
    spotify_limit.install(client)
    return client


def test_long_retry_after_fails_fast(server, _limit_state) -> None:
    httpd, queue, hits = server
    queue.append((429, {"Retry-After": "62939"}))
    client = _client(httpd)

    start = time.monotonic()
    with pytest.raises(SpotifyRateLimited, match=r"Spotify rate-limited: Retry-After 62939s, until \d{4}-\d\d-\d\d"):
        client.playlist("p", fields="snapshot_id")
    assert time.monotonic() - start < 5
    assert len(hits) == 1

    saved = json.loads(spotify_limit.STATE_FILE.read_text())
    until = datetime.fromisoformat(saved["until"])
    assert saved["retry_after"] == 62939
    assert abs((until - datetime.now(timezone.utc)).total_seconds() - 62939) < 5
    [(body, job)] = [c.args for c in _limit_state.call_args_list]
    assert job == "music_spotify"
    assert f"music_spotify_rate_limited_until_seconds {int(until.timestamp())}" in body


def test_short_retry_after_is_waited_out(server, _limit_state) -> None:
    httpd, queue, hits = server
    queue.append((429, {"Retry-After": "1"}))
    client = _client(httpd)

    assert client.playlist("p", fields="snapshot_id") == {"snapshot_id": "s1"}
    assert len(hits) == 2
    assert not spotify_limit.STATE_FILE.exists()
    _limit_state.assert_not_called()


def test_after_a_trip_no_request_reaches_spotify(server) -> None:
    """spotdl swallows per-track errors in the downloader; later calls must still fail."""
    httpd, queue, hits = server
    queue.append((429, {"Retry-After": "3600"}))
    client = _client(httpd)
    with pytest.raises(SpotifyRateLimited):
        client.playlist("p")

    with pytest.raises(SpotifyRateLimited, match="recorded earlier; not calling Spotify"):
        client.track("t")
    assert len(hits) == 1


def test_limit_recorded_by_an_earlier_run_is_honoured(server) -> None:
    httpd, _queue, hits = server
    until = datetime.now(timezone.utc) + timedelta(hours=3)
    spotify_limit.STATE_FILE.write_text(json.dumps({"until": until.isoformat(), "retry_after": 10800}))
    client = _client(httpd)

    with pytest.raises(SpotifyRateLimited, match="Retry-After 10800s"):
        client.playlist("p")
    assert hits == []


def test_expired_limit_lets_requests_through(server) -> None:
    httpd, _queue, hits = server
    until = datetime.now(timezone.utc) - timedelta(minutes=1)
    spotify_limit.STATE_FILE.write_text(json.dumps({"until": until.isoformat(), "retry_after": 60}))
    client = _client(httpd)

    assert client.playlist("p", fields="snapshot_id") == {"snapshot_id": "s1"}
    assert len(hits) == 1


def test_unreadable_state_is_ignored(server) -> None:
    httpd, _queue, hits = server
    spotify_limit.STATE_FILE.write_text("{not json")
    client = _client(httpd)

    assert client.playlist("p", fields="snapshot_id") == {"snapshot_id": "s1"}


def test_installs_on_spotdls_client_keeping_its_retry_settings() -> None:
    """Guards the private spotipy/spotdl attributes install() relies on: a bump that
    renames them must fail here, not silently bring back the 17h sleep."""
    from spotdl.utils.spotify import _OfficialSpotifyClient

    client = _OfficialSpotifyClient.init(client_id="id", client_secret="secret", no_cache=True)
    spotify_limit.install(client)
    spotify_limit.install(client)  # idempotent

    adapter = client._session.get_adapter("https://api.spotify.com/v1/playlists/p")
    assert isinstance(adapter, FailFastAdapter)
    retry = adapter.max_retries
    assert isinstance(retry, spotify_limit.FailFastRetry)
    assert 429 in retry.status_forcelist
    assert retry.total == client.retries
    # increment() rebuilds the Retry; the subclass must survive it.
    assert isinstance(retry.new(), spotify_limit.FailFastRetry)
    assert retry.new().retry_after_max > 62939

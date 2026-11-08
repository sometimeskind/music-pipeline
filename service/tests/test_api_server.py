"""The API server answers /health while tool calls wait (homelab#2701)."""

from __future__ import annotations

import threading
import time
import urllib.request

import pytest

from music_service.api import create_app
from music_service.cli import make_api_server

# More than waitress's default 4 threads: each /mcp-api/run call holds one
# for up to MCP_INLINE_SECONDS, and the short lane takes 5 jobs at once.
BLOCKED = 8


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("API_BEARER_TOKEN", "test-secret")
    app = create_app()
    release = threading.Event()
    entered = threading.Semaphore(0)

    @app.get("/test-block")
    def block():
        entered.release()
        release.wait(30)
        return "done"

    srv = make_api_server(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    clients: list[threading.Thread] = []
    yield srv, entered, clients
    # Let the held requests finish before the server goes away.
    release.set()
    for c in clients:
        c.join(10)
    srv.close()
    srv.task_dispatcher.shutdown(timeout=5)
    thread.join(5)


def _get(port, path, timeout):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer test-secret"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def test_health_answers_while_requests_are_blocked_in_handlers(server):
    srv, entered, clients = server
    port = srv.effective_port
    clients += [
        threading.Thread(target=lambda: _get(port, "/test-block", 35), daemon=True)
        for _ in range(BLOCKED)
    ]
    for c in clients:
        c.start()
    deadline = time.monotonic() + 5
    held = 0
    while held < BLOCKED and entered.acquire(timeout=max(0.0, deadline - time.monotonic())):
        held += 1

    # The k8s probes time out at 5 s.
    assert _get(port, "/health", timeout=3) == 200
    assert held == BLOCKED

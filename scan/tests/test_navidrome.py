"""Tests for pipeline.navidrome.trigger_scan()."""

from __future__ import annotations

import unittest.mock as mock

import pytest
import requests


def _make_subsonic_response(status: str = "ok") -> dict:
    return {"subsonic-response": {"status": status, "version": "1.8.0"}}


@pytest.fixture(autouse=True)
def clear_navidrome_env(monkeypatch):
    """Ensure Navidrome env vars are absent by default in every test."""
    monkeypatch.delenv("NAVIDROME_URL", raising=False)
    monkeypatch.delenv("NAVIDROME_USER", raising=False)
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)


def test_no_url_skips_http_call():
    """When NAVIDROME_URL is unset, no HTTP request is made."""
    with mock.patch("music_scan.navidrome.requests.get") as mock_get:
        from music_scan.navidrome import trigger_scan
        trigger_scan()
    mock_get.assert_not_called()


def test_url_without_credentials_raises(monkeypatch):
    """When URL is set but credentials are missing, RuntimeError is raised."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")

    with mock.patch("music_scan.navidrome.requests.get") as mock_get:
        from music_scan.navidrome import trigger_scan
        with pytest.raises(RuntimeError, match="NAVIDROME_USER or NAVIDROME_PASSWORD is missing"):
            trigger_scan()

    mock_get.assert_not_called()


def test_url_with_only_user_raises(monkeypatch):
    """When URL and user are set but password is missing, RuntimeError is raised."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")

    with mock.patch("music_scan.navidrome.requests.get") as mock_get:
        from music_scan.navidrome import trigger_scan
        with pytest.raises(RuntimeError, match="NAVIDROME_USER or NAVIDROME_PASSWORD is missing"):
            trigger_scan()

    mock_get.assert_not_called()


def test_successful_scan_logs_info(monkeypatch, caplog):
    """Happy path: Subsonic returns ok → info logged, correct endpoint called."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    mock_resp = mock.Mock()
    mock_resp.json.return_value = _make_subsonic_response("ok")

    with mock.patch("music_scan.navidrome.requests.get", return_value=mock_resp) as mock_get:
        import logging
        with caplog.at_level(logging.INFO, logger="music_scan.navidrome"):
            from music_scan.navidrome import trigger_scan
            trigger_scan()

    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args
    assert call_kwargs[0][0] == "http://navidrome.example.com/rest/startScan.view"
    assert call_kwargs[1]["params"]["u"] == "admin"
    assert call_kwargs[1]["params"]["f"] == "json"
    assert "Navidrome library rescan triggered" in caplog.text


def test_trailing_slash_in_url_is_normalized(monkeypatch):
    """A trailing slash in NAVIDROME_URL must not produce a double slash."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com/")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    mock_resp = mock.Mock()
    mock_resp.json.return_value = _make_subsonic_response("ok")

    with mock.patch("music_scan.navidrome.requests.get", return_value=mock_resp) as mock_get:
        from music_scan.navidrome import trigger_scan
        trigger_scan()

    url_called = mock_get.call_args[0][0]
    assert "//" not in url_called.replace("http://", "")


def test_non_ok_subsonic_status_raises(monkeypatch):
    """When Subsonic returns a non-ok status, RuntimeError is raised."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    mock_resp = mock.Mock()
    mock_resp.json.return_value = _make_subsonic_response("failed")

    with mock.patch("music_scan.navidrome.requests.get", return_value=mock_resp):
        from music_scan.navidrome import trigger_scan
        with pytest.raises(RuntimeError, match="non-ok status"):
            trigger_scan()


def test_http_error_raises(monkeypatch):
    """When requests raises a RequestException, RuntimeError is raised."""
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")

    with mock.patch(
        "music_scan.navidrome.requests.get",
        side_effect=requests.ConnectionError("connection refused"),
    ):
        from music_scan.navidrome import trigger_scan
        with pytest.raises(RuntimeError, match="Failed to trigger Navidrome rescan"):
            trigger_scan()


# ---------------------------------------------------------------------------
# sweep_playlists (homelab#2541)
# ---------------------------------------------------------------------------


@pytest.fixture
def navidrome_env(monkeypatch):
    monkeypatch.setenv("NAVIDROME_URL", "http://navidrome.example.com/")
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")
    monkeypatch.delenv("NAVIDROME_PLAYLISTS_DIR", raising=False)


def _resp(body=None, error: Exception | None = None) -> mock.Mock:
    resp = mock.Mock()
    resp.json.return_value = body
    resp.raise_for_status.side_effect = error
    return resp


def _playlist(pid: str, name: str, path: str, sync: bool = True) -> dict:
    return {"id": pid, "name": name, "path": path, "sync": sync, "ownerName": "admin"}


class FakeNavidrome:
    """requests.post/get/delete against a Navidrome holding *playlists*."""

    def __init__(self, playlists, is_admin=True, fail_delete=()):
        self.playlists = playlists
        self.is_admin = is_admin
        self.fail_delete = set(fail_delete)
        self.deleted: list[str] = []
        self.headers: list[dict] = []

    def post(self, url, json=None, timeout=None):
        assert url == "http://navidrome.example.com/auth/login"
        assert json == {"username": "admin", "password": "secret"}
        return _resp({"token": "jwt", "isAdmin": self.is_admin, "username": "admin"})

    def get(self, url, headers=None, timeout=None):
        assert url == "http://navidrome.example.com/api/playlist"
        self.headers.append(headers)
        return _resp(self.playlists)

    def delete(self, url, headers=None, timeout=None):
        self.headers.append(headers)
        pid = url.rsplit("/", 1)[1]
        assert url == f"http://navidrome.example.com/api/playlist/{pid}"
        if pid in self.fail_delete:
            return _resp(error=requests.HTTPError("403 Forbidden"))
        self.deleted.append(pid)
        return _resp({})

    def patch(self):
        return mock.patch.multiple("music_scan.navidrome.requests", post=self.post, get=self.get,
                                   delete=self.delete)


def test_sweep_deletes_by_path_and_keeps_a_hand_made_playlist_of_the_same_name(navidrome_env, tmp_path, caplog):
    """The playlist imported from keep.m3u goes once the file is gone; a
    playlist made in Navidrome named keep has no path and stays."""
    (tmp_path / "mix.m3u").write_text("")
    nd = FakeNavidrome([
        _playlist("imported", "keep", "/music/playlists/keep.m3u"),
        _playlist("handmade", "keep", "", sync=False),
        _playlist("current", "mix", "/music/playlists/mix.m3u"),
    ])
    from music_scan.navidrome import sweep_playlists
    import logging
    with nd.patch(), caplog.at_level(logging.INFO, logger="music_scan.navidrome"):
        assert sweep_playlists(tmp_path, keep={"mix"}) == ["keep"]
    assert nd.deleted == ["imported"]
    assert all(h == {"X-ND-Authorization": "Bearer jwt"} for h in nd.headers)
    assert "deleted playlist keep: imported from /music/playlists/keep.m3u, which is gone" in caplog.text


def test_sweep_leaves_conf_playlists_sync_off_and_files_outside_playlists(navidrome_env, tmp_path):
    nd = FakeNavidrome([
        _playlist("conf", "mix", "/music/playlists/mix.m3u"),  # in playlists.conf: its file is due back
        _playlist("unsynced", "old", "/music/playlists/old.m3u", sync=False),  # the operator took it over
        _playlist("library", "album", "/music/library/A/B/album.m3u"),  # not under playlists/
    ])
    from music_scan.navidrome import sweep_playlists
    with nd.patch():
        assert sweep_playlists(tmp_path, keep={"mix"}) == []
    assert nd.deleted == []


def test_sweep_follows_navidrome_playlists_dir(navidrome_env, monkeypatch, tmp_path):
    monkeypatch.setenv("NAVIDROME_PLAYLISTS_DIR", "/srv/playlists")
    nd = FakeNavidrome([
        _playlist("a", "gone", "/music/playlists/gone.m3u"),
        _playlist("b", "gone", "/srv/playlists/gone.m3u"),
    ])
    from music_scan.navidrome import sweep_playlists
    with nd.patch():
        assert sweep_playlists(tmp_path, keep=set()) == ["gone"]
    assert nd.deleted == ["b"]


def test_sweep_logs_a_failed_delete_and_goes_on(navidrome_env, tmp_path, caplog):
    nd = FakeNavidrome([
        _playlist("a", "one", "/music/playlists/one.m3u"),
        _playlist("b", "two", "/music/playlists/two.m3u"),
    ], fail_delete={"a"})
    from music_scan.navidrome import sweep_playlists
    with nd.patch():
        assert sweep_playlists(tmp_path, keep=set()) == ["two"]
    assert "could not delete playlist one" in caplog.text


def test_sweep_warns_when_the_user_is_not_an_admin(navidrome_env, tmp_path, caplog):
    nd = FakeNavidrome([], is_admin=False)
    from music_scan.navidrome import sweep_playlists
    with nd.patch():
        assert sweep_playlists(tmp_path, keep=set()) == []
    assert "admin is not a Navidrome admin" in caplog.text


def test_sweep_raises_when_navidrome_cant_be_listed(navidrome_env, tmp_path):
    nd = FakeNavidrome([])
    from music_scan.navidrome import sweep_playlists
    with nd.patch(), mock.patch("music_scan.navidrome.requests.get",
                                side_effect=requests.ConnectionError("refused")):
        with pytest.raises(RuntimeError, match="Could not list Navidrome playlists"):
            sweep_playlists(tmp_path, keep=set())


def test_sweep_raises_when_login_fails(navidrome_env, tmp_path):
    from music_scan.navidrome import sweep_playlists
    with mock.patch("music_scan.navidrome.requests.post",
                    return_value=_resp(error=requests.HTTPError("401"))):
        with pytest.raises(RuntimeError, match="Navidrome login failed"):
            sweep_playlists(tmp_path, keep=set())


def test_sweep_without_navidrome_or_playlists_dir_does_nothing(navidrome_env, monkeypatch, tmp_path):
    from music_scan.navidrome import sweep_playlists
    with mock.patch("music_scan.navidrome.requests") as mock_requests:
        assert sweep_playlists(tmp_path / "missing", keep=set()) == []
        monkeypatch.delenv("NAVIDROME_URL")
        assert sweep_playlists(tmp_path, keep=set()) == []
    assert not mock_requests.mock_calls

#!/usr/bin/env python3
"""Spike for the two-way playlist sync (homelab#2361, step 1). Throwaway: delete it once
the findings are on the issue.

Settles what the sync engine depends on, without writing to Spotify:
  spotify   user OAuth (authorization-code grant, loopback redirect), a refresh, every
            configured playlist read under the user token and under client credentials,
            and one isrc: search
  navidrome a login as c=music-playlist-sync: whether songs report real paths and isrc,
            how imported (.m3u) playlists are marked, getScanStatus; with --navidrome-write
            also a scratch API-owned playlist that is created, edited and deleted

Runs on the workstation, from the repo's .venv (requests only):
  op run --env-file .env.tpl -- .venv/bin/python scripts/spike-playlist-sync.py all

Reads SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, NAVIDROME_USER, NAVIDROME_PASSWORD and
optionally NAVIDROME_URL (default https://music.prins.id). The redirect URI it prints must
be registered on the Spotify app first. The user token is a write credential to the
Spotify account: it is written 0600 to --token and never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)
CLIENT = "music-playlist-sync"


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"{name} is not set")
    return value


def load_conf(paths: list[Path]) -> list[tuple[str, str, list[str]]]:
    """Return (name, playlist id, flags) for every line of the playlists.conf files."""
    out = []
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            parts = raw.split("#")[0].split()
            if len(parts) >= 2:
                pid = parts[1].rstrip("/").split("/")[-1].split("?")[0]
                out.append((parts[0], pid, parts[2:]))
    return out


# --- Spotify ---------------------------------------------------------------


def _token_request(data: dict) -> dict:
    resp = requests.post(
        f"{ACCOUNTS}/api/token",
        data=data,
        auth=(env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_CLIENT_SECRET")),
        timeout=15,
    )
    if resp.status_code != 200:
        # The error body names the problem (invalid_grant, redirect mismatch), not the secret.
        sys.exit(f"token request failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()


def _save_token(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(token, fh)


def consent(port: int) -> dict:
    """Run the authorization-code consent through a one-shot loopback server."""
    redirect = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(16)
    url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode(
        {
            "client_id": env("SPOTIFY_CLIENT_ID"),
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": SCOPES,
            "state": state,
        }
    )
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done, you can close this tab.")

        def log_message(self, *args):
            pass

    print(f"Redirect URI (must be registered on the Spotify app): {redirect}")
    print(f"Opening the consent page; if no browser opens, visit:\n  {url}")
    webbrowser.open(url)
    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        while "code" not in got and "error" not in got:
            server.handle_request()
    if got.get("state") != state:
        sys.exit("consent failed: state mismatch")
    if "error" in got:
        sys.exit(f"consent failed: {got['error']}")
    token = _token_request(
        {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect}
    )
    token["obtained_at"] = int(time.time())
    return token


def user_token(path: Path, port: int) -> str:
    """Consent once, then refresh: the refresh is what the engine does every hour."""
    if path.exists():
        token = json.loads(path.read_text())
        print(f"Using the saved token at {path}")
    else:
        token = consent(port)
        print(f"Consent OK, granted scopes: {token.get('scope')}")
    fresh = _token_request(
        {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
    )
    rotated = "refresh_token" in fresh and fresh["refresh_token"] != token["refresh_token"]
    token.update(fresh, obtained_at=int(time.time()))
    _save_token(path, token)
    print(f"Refresh OK (refresh token rotated: {rotated}); token saved 0600 to {path}")
    return token["access_token"]


def app_token() -> str:
    return _token_request({"grant_type": "client_credentials"})["access_token"]


def api_get(token: str, url: str, **params) -> requests.Response:
    return requests.get(
        url if url.startswith("http") else f"{API}{url}",
        params=params or None,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def read_items(token: str, pid: str) -> tuple[str, list[dict]]:
    """Page through a playlist; return the endpoint that answered and the entries."""
    for endpoint in ("items", "tracks"):
        resp = api_get(token, f"/playlists/{pid}/{endpoint}", limit=50)
        if resp.status_code == 200:
            break
    else:
        return f"HTTP {resp.status_code} {resp.text[:200]}", []
    entries = []
    while True:
        page = resp.json()
        entries.extend(page.get("items") or [])
        if not page.get("next"):
            return endpoint, entries
        resp = api_get(token, page["next"])
        resp.raise_for_status()


def describe(entries: list[dict]) -> dict:
    """Count the entry shapes the merge has to handle."""
    shape: dict = {"total": len(entries), "keys": set(), "null": 0, "local": 0, "isrc": 0}
    for entry in entries:
        shape["keys"].add("item" if "item" in entry else "track" if "track" in entry else "?")
        track = entry.get("item") or entry.get("track")
        if not track:
            shape["null"] += 1
        elif entry.get("is_local") or track.get("is_local"):
            shape["local"] += 1
        elif (track.get("external_ids") or {}).get("isrc"):
            shape["isrc"] += 1
    shape["keys"] = sorted(shape["keys"])
    return shape


def spotify(args, conf) -> None:
    user = user_token(args.token, args.port)
    me = api_get(user, "/me").json()
    print(f"User: {me.get('id')} ({me.get('product')})")
    app = app_token()
    probe = None
    for name, pid, flags in conf:
        meta = api_get(user, f"/playlists/{pid}", fields="name,snapshot_id,owner.id,collaborative")
        info = meta.json() if meta.status_code == 200 else {"error": meta.status_code}
        u_endpoint, u_entries = read_items(user, pid)
        a_endpoint, a_entries = read_items(app, pid)
        print(f"\n[{name}] flags={flags or '-'} owner={info.get('owner', {}).get('id')} "
              f"collaborative={info.get('collaborative')} snapshot={info.get('snapshot_id')}")
        print(f"  user token: /{u_endpoint} {describe(u_entries)}")
        print(f"  app token:  /{a_endpoint} {describe(a_entries)}")
        for entry in u_entries:
            track = entry.get("item") or entry.get("track") or {}
            isrc = (track.get("external_ids") or {}).get("isrc")
            if probe is None and isrc:
                probe = (track["id"], isrc)
    if probe:
        tid, isrc = probe
        resp = api_get(user, "/search", q=f"isrc:{isrc}", type="track", limit=10)
        ids = [t["id"] for t in resp.json().get("tracks", {}).get("items", [])] if resp.ok else []
        print(f"\nisrc:{isrc} search: HTTP {resp.status_code}, {len(ids)} hit(s), "
              f"source track among them: {tid in ids}")


# --- Navidrome -------------------------------------------------------------


def nd(method: str, **params) -> dict:
    salt = secrets.token_hex(8)
    password = env("NAVIDROME_PASSWORD")
    base = os.environ.get("NAVIDROME_URL", "https://music.prins.id").rstrip("/")
    query = [
        ("u", env("NAVIDROME_USER")),
        ("t", hashlib.md5((password + salt).encode()).hexdigest()),  # noqa: S324 - Subsonic auth
        ("s", salt),
        ("v", "1.16.1"),
        ("c", CLIENT),
        ("f", "json"),
    ]
    for key, value in params.items():
        for v in value if isinstance(value, list) else [value]:
            query.append((key, v))
    resp = requests.get(f"{base}/rest/{method}", params=query, timeout=30)
    resp.raise_for_status()
    body = resp.json()["subsonic-response"]
    if body.get("status") != "ok":
        raise RuntimeError(f"{method}: {body.get('error')}")
    return body


def song_summary(song: dict) -> str:
    path = song.get("path", "")
    kind = "real" if path.startswith("/") else "fake"
    return f"id={song.get('id')} path[{kind}]={path!r} isrc={song.get('isrc')}"


def navidrome(args, conf) -> None:
    ping = nd("ping")
    print(f"\nNavidrome: {ping.get('type')} {ping.get('serverVersion')} "
          f"openSubsonic={ping.get('openSubsonic')}")
    print(f"getScanStatus: {nd('getScanStatus').get('scanStatus')}")
    playlists = nd("getPlaylists").get("playlists", {}).get("playlist", [])
    names = {name for name, _, _ in conf}
    target = None
    for pl in playlists:
        extra = {k: v for k, v in pl.items() if k not in ("coverArt", "comment")}
        print(f"  playlist {extra}")
        if pl.get("name") in names and target is None:
            target = pl
    if target is None:
        print("No Navidrome playlist matches a configured name")
        return
    songs = nd("getPlaylist", id=target["id"]).get("playlist", {}).get("entry", [])
    print(f"\n[{target['name']}] {len(songs)} songs; first three:")
    for song in songs[:3]:
        print(f"  {song_summary(song)}")
    if args.navidrome_write and len(songs) >= 3:
        scratch(songs)


def scratch(songs: list[dict]) -> None:
    """Create, edit, read back and delete an API-owned playlist."""
    name = f"spike-sync-{int(time.time())}"
    created = nd("createPlaylist", name=name, songId=[s["id"] for s in songs[:2]])
    pid = created.get("playlist", {}).get("id")
    if not pid:  # servers on the 1.8 API return no body; look it up by name
        pid = next(p["id"] for p in nd("getPlaylists")["playlists"]["playlist"]
                   if p["name"] == name)
    try:
        def ids():
            return [e["id"] for e in nd("getPlaylist", id=pid)["playlist"].get("entry", [])]

        print(f"\nScratch playlist {name} ({pid}) created: {ids()}")
        nd("updatePlaylist", playlistId=pid, songIdToAdd=songs[2]["id"], songIndexToRemove=0)
        print(f"  after add #3, remove index 0: {ids()}")
        own = next(p for p in nd("getPlaylists")["playlists"]["playlist"] if p["id"] == pid)
        print(f"  listed as {({k: v for k, v in own.items() if k != 'coverArt'})}")
    finally:
        nd("deletePlaylist", id=pid)
        print(f"  deleted {pid}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("part", choices=["spotify", "navidrome", "all"])
    parser.add_argument("--conf", type=Path, action="append",
                        help="playlists.conf file(s) (default: config/playlists.conf)")
    parser.add_argument("--token", type=Path, default=Path(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "music-playlist-sync", "spotify-token.json"))
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--navidrome-write", action="store_true",
                        help="also create, edit and delete a scratch playlist")
    args = parser.parse_args()
    conf = load_conf(args.conf or [Path("config/playlists.conf")])
    if args.part in ("spotify", "all"):
        spotify(args, conf)
    if args.part in ("navidrome", "all"):
        navidrome(args, conf)


if __name__ == "__main__":
    main()

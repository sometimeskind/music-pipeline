"""A seeded /mcp-api/* app for tests: this service's and the MCP adapter's contract test (homelab#2412).

:func:`seed` writes a small beets library, an album store, a playlist store
and a quarantine under a directory; :func:`make_app` points the service at
them and builds the app around a :class:`JobQueue` whose commands are faked
(:class:`Commands`), so no CLI ever runs.  Run it as a server:

    python service/tests/mcp_harness.py --port 8080 --dir /tmp/mcp   # /app/tests/mcp_harness.py in the dev image

``MCP_API_TOKEN`` defaults to ``mcp-secret``.  A faked command prints
``ran <argv>`` and exits 0 unless :attr:`Commands.outputs` says otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path

ALBUMS = {
    "ALB1": {"artist": "The Beatles", "name": "Abbey Road", "status": "imported", "playlists": {"albums": []}},
    "ALB2": {"artist": "The Beatles", "name": "Abbey Road (Remastered)", "status": "wanted", "playlists": {}},
    "ALB3": {"artist": "Nina Simone", "name": "Pastel Blues", "status": "failed", "playlists": {}},
}


class Commands:
    """A fake ``run_command``: *outputs* maps an argv, joined by spaces, to (lines, exit code)."""

    def __init__(self) -> None:
        self.outputs: dict[str, tuple[list[str], int]] = {}
        self.ran: list[list[str]] = []

    def __call__(self, argv, env, on_line):
        self.ran.append(argv)
        lines, code = self.outputs.get(" ".join(argv), ([f"ran {' '.join(argv)}"], 0))
        for line in lines:
            on_line(line)
        return code


def seed(root: Path) -> dict:
    """Write the fixtures under *root*. Returns their paths and the library items."""
    from beets.library import Item, Library

    root.mkdir(parents=True, exist_ok=True)
    libdir = root / "library"
    db = root / "library.db"
    db.unlink(missing_ok=True)
    lib = Library(str(db), directory=str(libdir))
    items = []
    for n, (title, sid, isrc) in enumerate([("Come Together", "S1", "GB1"), ("Something", "S2", ""),
                                            ("Loose", "", "GB9")], 1):
        item = Item(title=title, artist="The Beatles", album="Abbey Road", track=n,
                    path=str(libdir / "The Beatles" / "Abbey Road" / f"0{n} - {title}.m4a").encode())
        if sid:
            item["spotify_ids"] = sid
            item["spotify_album_id"] = "ALB1"
            item["via"] = "usenet"
        if isrc:
            item["isrc"] = isrc
        lib.add(item)
        items.append(item)
    lib._close()

    spotdl = root / "spotdl"
    stores = spotdl / ".store"
    stores.mkdir(parents=True, exist_ok=True)
    state = spotdl / ".albums.json"
    state.write_text(json.dumps({"albums": ALBUMS}))
    (stores / "chill.json").write_text(json.dumps({
        "playlist": "chill", "updated": "2026-10-27T03:10:00Z", "source": "spotify", "entries": [
            {"song": {"name": "Come Together", "artists": ["The Beatles"], "album_name": "Abbey Road",
                      "song_id": "S1", "isrc": "GB1"}},
            {"song": {"name": "Loose one", "artists": ["The Beatles"], "song_id": "SX", "isrc": "GB9"}},
            {"song": {"name": "Absent", "artists": ["Someone"], "song_id": "SY"}},
            {"local": {"id": items[1].id, "title": "Something", "artist": "The Beatles"}},
        ]}))
    quarantine = root / "quarantine"
    for rel in ["Artist/one.m4a", "rejected/chill/two.m4a", "replaced/3-three.m4a", "replaced/4-four.m4a"]:
        (quarantine / rel).parent.mkdir(parents=True, exist_ok=True)
        (quarantine / rel).write_bytes(b"x")
    return {"root": root, "db": db, "libdir": libdir, "state": state, "stores": stores,
            "quarantine": quarantine, "jobs_file": spotdl / ".mcp-jobs.json", "items": items}


def patches(seeded: dict) -> tuple[list[tuple[object, str, object]], dict[str, str]]:
    """The (module, attribute, value) patches and the environment that point the service at *seeded*."""
    import music_fetch.albums as albums
    import music_fetch.store as store
    import music_scan.library as library

    return ([(library, "LIBRARY_DB", seeded["db"]), (library, "LIBRARY_DIR", seeded["libdir"]),
             (albums, "STATE_FILE", seeded["state"]), (store, "STORE_DIR", seeded["stores"])],
            {"MUSIC_QUARANTINE": str(seeded["quarantine"])})


def make_app(seeded: dict, commands: Commands | None = None):
    """The app over *seeded* (patches applied by the caller) and its job queue."""
    from music_service.api import create_app
    from music_service.mcp_jobs import JobQueue

    jobs = JobQueue(seeded["jobs_file"], run_command=commands or Commands(),
                    locks=lambda names: contextlib.nullcontext())
    app = create_app(mcp_jobs=jobs)
    app.config["TESTING"] = True
    return app, jobs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dir", type=Path, required=True, help="where to write the fixtures")
    args = parser.parse_args()
    seeded = seed(args.dir)
    attrs, env = patches(seeded)
    for module, name, value in attrs:
        setattr(module, name, value)
    os.environ.update(env)
    os.environ.setdefault("MCP_API_TOKEN", "mcp-secret")
    os.environ.setdefault("API_BEARER_TOKEN", "api-secret")
    app, _ = make_app(seeded)
    import waitress

    print(f"mcp harness on http://{args.host}:{args.port}", flush=True)
    waitress.serve(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

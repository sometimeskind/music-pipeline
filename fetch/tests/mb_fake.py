"""A fake MusicBrainz for the album-request tests (homelab#2478): one release
group and its releases.

Imported by the tests that use it (``from mb_fake import FakeMB``), as
pg_fixture is: test modules don't import one another (CI's importlib mode).
"""

from __future__ import annotations

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"
OTHER_RG = "0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10"
R12 = "11111111-1111-4111-8111-111111111111"   # 12 tracks, 2001
R12B = "22222222-2222-4222-8222-222222222222"  # 12 tracks, 2005
R14 = "33333333-3333-4333-8333-333333333333"   # 14 tracks, 1999: earliest, but the rarer count
RLIVE = "44444444-4444-4444-8444-444444444444"
RBOOT = "55555555-5555-4555-8555-555555555555"


def _medium(n: int, position: int = 1, fmt: str = "CD", isrc: bool = True) -> dict:
    return {"position": position, "format": fmt, "track-count": n, "tracks": [
        {"position": i, "title": f"Song {i}", "length": 200_000,
         "recording": {"title": f"Song {i}", "isrcs": [f"GBAAA00{position}{i:03d}"] if isrc else []}}
        for i in range(1, n + 1)]}


def _release(rid: str, n: int, date: str, status: str = "Official", group: str = RG, **extra) -> dict:
    return {"id": rid, "title": "The Album", "status": status, "date": date, "country": "GB",
            "media": [_medium(n)], "release-group": {"id": group}, **extra}


GROUP = {"id": RG, "title": "The Album", "primary-type": "Album", "secondary-types": [],
         "first-release-date": "1999-03-01",
         "artist-credit": [{"name": "Band", "joinphrase": " & "}, {"name": "Friend", "joinphrase": ""}]}
RELEASES = {
    R12: _release(R12, 12, "2001-01-01"),
    R12B: _release(R12B, 12, "2005-01-01"),
    R14: _release(R14, 14, "1999-03-01"),
    RLIVE: _release(RLIVE, 14, "2002-01-01", disambiguation="live in Tokyo"),
    RBOOT: _release(RBOOT, 14, "1998-01-01", status="Bootleg"),
}


class FakeMB:
    def __init__(self, group: dict | None = GROUP, releases: dict | None = None) -> None:
        self.group = group
        self.releases = RELEASES if releases is None else releases
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, **params) -> dict | None:
        self.calls.append((path, params))
        if path == f"release-group/{RG}":
            return self.group
        if path.startswith("release-group/"):
            return None
        if path == "release":
            assert params["release-group"] == RG and params["inc"] == "media"
            listed = list(self.releases.values())
            offset = int(params["offset"])
            return {"release-count": len(listed), "releases": listed[offset:offset + int(params["limit"])]}
        return self.releases.get(path.removeprefix("release/"))

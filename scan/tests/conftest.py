"""Shared fixtures for the scan tests."""

import shutil
import subprocess
from pathlib import Path

import pytest

SPOTIFY_URL = "https://open.spotify.com/track/abc123"


def _clip(path: Path, *parts: tuple[str, float], url: str | None = SPOTIFY_URL) -> Path:
    """Concatenate ``("tone"|"silence", seconds)`` parts into an AAC m4a tagged like spotdl."""
    args, labels = ["ffmpeg", "-v", "error", "-y"], ""
    for i, (kind, secs) in enumerate(parts):
        src = f"sine=frequency=440:duration={secs}" if kind == "tone" else f"anullsrc=r=44100:cl=mono:d={secs}"
        args += ["-f", "lavfi", "-i", src]
        labels += f"[{i}:a]aresample=44100,aformat=channel_layouts=mono[a{i}];"
    concat = "".join(f"[a{i}]" for i in range(len(parts)))
    args += ["-filter_complex", f"{labels}{concat}concat=n={len(parts)}:v=0:a=1[out]",
             "-map", "[out]", "-c:a", "aac", str(path)]
    subprocess.run(args, check=True)
    if url:
        from mutagen.mp4 import MP4, MP4FreeForm

        audio = MP4(path)
        audio["----:spotdl:WOAS"] = [MP4FreeForm(url.encode())]
        audio.save()
    return path


@pytest.fixture
def clip():
    """Generate an AAC m4a from lavfi tone and silence parts, tagged like spotdl.

    Skips where ffmpeg is missing (the bare CI runner); the dev image has it."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg not installed")
    return _clip

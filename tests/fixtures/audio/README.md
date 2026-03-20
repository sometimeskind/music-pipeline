# Test Audio Fixture

The test suite generates its own audio fixture at runtime using ffmpeg — no file
needs to be provided manually, and nothing is committed here.

## What gets generated

A **silent MP3** with embedded metadata matching the real recording:

| Field | Value |
|---|---|
| Artist | Nine Inch Nails |
| Title | 7 Ghosts I |
| Album | Ghosts I-IV |
| Duration | 121 s (matches MusicBrainz track length) |
| MusicBrainz Recording ID | [1d1bb32a-5bc6-4b6f-88cc-c043f6c52509](https://musicbrainz.org/recording/1d1bb32a-5bc6-4b6f-88cc-c043f6c52509) |
| License | [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) |

## Why synthetic audio works

The import tests use a test-specific beets config (`beets_test_config` fixture) with:
- `strong_rec_thresh: 0.30` — accepts a good MusicBrainz text match
- `chroma` plugin removed — no AcoustID fingerprint lookups

With embedded `artist`, `title`, and `album` tags, plus a matching duration, beets
gets enough signal from a MusicBrainz text search to achieve high confidence on this
unique title/artist combination — no real audio signal required.

## How it's generated

`ffmpeg` runs inside the scan container (which already ships ffmpeg) and writes the
file to a host-side `tmp_path_factory` directory. The `fixture_audio` pytest fixture
is session-scoped, so generation happens once per test run.

```bash
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo \
  -t 121 \
  -metadata artist="Nine Inch Nails" \
  -metadata title="7 Ghosts I" \
  -metadata album="Ghosts I-IV" \
  -q:a 9 -y "Nine Inch Nails - 7 Ghosts I.mp3"
```

## Attribution (CC BY-NC-SA 3.0)

"7 Ghosts I" by Nine Inch Nails is licensed under
[Creative Commons Attribution-NonCommercial-ShareAlike 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/).
The synthesized file is used solely as a metadata carrier for non-commercial
test infrastructure.
